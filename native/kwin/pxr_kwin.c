// Pocket XR on KDE Plasma (Wayland): the screen's and app windows' pictures, the window list and the pointer,
// through KWin's own Wayland interfaces.
//
// On a Wayland desktop an app cannot read the screen, list other apps' windows or move the pointer itself. KWin
// offers all three to a program that a desktop file names in X-KDE-Wayland-Interfaces (install.sh writes
// pocket-xr-kwin.desktop with Exec= this program), with no dialog:
//   zkde_screencast_unstable_v1        a monitor's or one window's picture as a PipeWire stream, the pointer as
//                                      metadata (KWin's own screen sharing, xdg-desktop-portal-kde, uses it)
//   org_kde_plasma_window_management  the windows: uuid, title, app, pid, place, state; activate and close
//                                      (Plasma's task manager uses it)
//   org_kde_kwin_fake_input           pointer and keys (KDE Connect's remote input uses it)
// And the ordinary wl_output and xdg_output for the monitors.
//
// Each stream's newest frame goes into its own shared memory file, laid out as pxr-mutter's (pocketxr/kwin.py reads
// it): u32 magic 'PXRM', u32 seq (odd while being written), u32 width, u32 height, i32 cursor x, i32 cursor y,
// u32 cursor seen (1: the pointer is on this picture now), u32 capacity, u32 frame number (changes only with a new
// picture), then BGRx at offset 64.
//
// stdin, one command per line:
//   record KEY output CONNECTOR   the picture of a monitor, KEY a short name of the app's choosing (no spaces)
//   record KEY window UUID        the picture of one window (its uuid from the window list)
//   record KEY region X Y W H S   the picture of a part of the desktop (logical pixels, at scale S)
//   stop KEY                      end that picture
//   pictures KEY 0|1              0: keep only the pointer's place (no copy of each frame), 1: pictures again
//   move X Y                      the pointer to (X, Y) on the desktop, in logical pixels
//   button CODE 1|0               a pointer button down or up (evdev code, 272 left)
//   key CODE 1|0                  a key down or up (evdev code, 56 left Alt)
//   activate UUID                 bring that window to the front (unminimising it)
//   close UUID                    close it, as its close button
//   monitors | windows            print the list again
//   quit
// stdout, one JSON object per line: {"caps": ...}, {"monitors": [...]}, {"windows": [...], "stacking": [...]},
// {"shm": path, "key": KEY}, {"format": ..., "key": KEY}, {"stream": state, "key": KEY}, {"closed": KEY},
// {"failed": KEY, "error": text}, {"error": text}.
// GPLv3.

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <poll.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

#include <pipewire/pipewire.h>
#include <spa/buffer/meta.h>
#include <spa/param/buffers.h>
#include <spa/param/param.h>
#include <spa/param/video/format-utils.h>
#include <wayland-client.h>

#include "fake-input-client-protocol.h"
#include "plasma-window-management-client-protocol.h"
#include "xdg-output-unstable-v1-client-protocol.h"
#include "zkde-screencast-unstable-v1-client-protocol.h"

#define SHM_HEADER 64
#define SHM_CAPACITY (64u * 1024u * 1024u)  // up to 4096x4096 BGRx
#define MAGIC 0x4D525850u                    // "PXRM"
#define CURSOR_META_SIZE(w, h) \
    ((int)(sizeof(struct spa_meta_cursor) + sizeof(struct spa_meta_bitmap) + (w) * (h) * 4))
#define POINTER_HIDDEN 1
#define POINTER_METADATA 4
#define MAX_STREAMS 32

// ---------------------------------------------------------------- output

static void out(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void out(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    vprintf(fmt, ap);
    va_end(ap);
    putchar('\n');
    fflush(stdout);
}

// A JSON string's inside (no quotes), into a static ring of buffers: up to 8 in one out() call.
static const char *js(const char *s) {
    static char ring[8][1024];
    static int at;
    char *o = ring[at++ & 7];
    size_t n = 0;
    for (const unsigned char *p = (const unsigned char *)(s ? s : ""); *p && n < sizeof ring[0] - 8; p++) {
        if (*p == '"' || *p == '\\') {
            o[n++] = '\\';
            o[n++] = (char)*p;
        } else if (*p < 0x20) {
            n += (size_t)snprintf(o + n, 8, "\\u%04x", *p);
        } else {
            o[n++] = (char)*p;
        }
    }
    o[n] = 0;
    return o;
}

static double now_s(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (double)t.tv_sec + t.tv_nsec / 1e9;
}

// ---------------------------------------------------------------- Wayland state

static struct wl_display *dpy;
static struct wl_registry *registry;
static struct zkde_screencast_unstable_v1 *screencast;
static uint32_t screencast_version;
static struct org_kde_plasma_window_management *wm;
static uint32_t wm_version;
static struct org_kde_kwin_fake_input *fake;
static uint32_t fake_version;
static struct zxdg_output_manager_v1 *xdg_outputs;

struct output {
    struct wl_list link;
    uint32_t name;  // the registry's
    struct wl_output *wl;
    struct zxdg_output_v1 *xdg;
    char connector[64], description[256], make[128], model[128];
    int x, y, lw, lh;  // logical place and size (xdg_output)
    int gx, gy;        // wl_output's own place, when there is no xdg_output
    int w, h;          // the current mode, in pixels
    int scale;
    double rate;
};
static struct wl_list outputs;
static int monitors_dirty;

struct window {
    struct wl_list link;
    struct org_kde_plasma_window *pw;
    char uuid[128], title[512], app_id[256], resource[256];
    uint32_t state, pid;
    int x, y, w, h;
    int ready;  // initial_state came
};
static struct wl_list windows;
static char *stacking;  // ;-separated uuids, bottom to top
static int windows_dirty;

// ---------------------------------------------------------------- streams

struct stream {
    int used;
    char key[200];
    char source[272];  // what it shows, for the log
    struct zkde_screencast_stream_unstable_v1 *k;
    uint32_t node;
    uint64_t serial;
    struct pw_stream *pw;
    struct spa_hook hook;
    struct spa_video_info_raw info;
    char shm_path[128];
    uint8_t *shm;
    int cursor_x, cursor_y, cursor_seen;
    uint32_t frame_number;
    atomic_int pictures;
};
static struct stream streams[MAX_STREAMS];
static int shm_counter;

static struct pw_thread_loop *pw_loop;
static struct pw_context *pw_ctx;
static struct pw_core *pw_core_;

static struct stream *find_stream(const char *key) {
    for (int i = 0; i < MAX_STREAMS; i++)
        if (streams[i].used && strcmp(streams[i].key, key) == 0) return &streams[i];
    return NULL;
}

static int shm_open_stream(struct stream *s) {
    if (s->shm) return 0;
    snprintf(s->shm_path, sizeof s->shm_path, "/dev/shm/pxr-kwin-%d-%d", (int)getpid(), shm_counter++);
    int fd = open(s->shm_path, O_RDWR | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
    if (fd < 0) return -1;
    if (ftruncate(fd, SHM_HEADER + SHM_CAPACITY) < 0) {
        close(fd);
        unlink(s->shm_path);
        return -1;
    }
    s->shm = mmap(NULL, SHM_HEADER + SHM_CAPACITY, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    close(fd);
    if (s->shm == MAP_FAILED) {
        s->shm = NULL;
        unlink(s->shm_path);
        return -1;
    }
    uint32_t *h = (uint32_t *)s->shm;
    h[0] = MAGIC;
    h[1] = 0;
    h[7] = SHM_CAPACITY;
    return 0;
}

static void on_param_changed(void *data, uint32_t id, const struct spa_pod *param) {
    struct stream *s = data;
    if (param == NULL || id != SPA_PARAM_Format) return;
    uint32_t media_type, media_subtype;
    if (spa_format_parse(param, &media_type, &media_subtype) < 0) return;
    if (media_type != SPA_MEDIA_TYPE_video || media_subtype != SPA_MEDIA_SUBTYPE_raw) return;
    spa_format_video_raw_parse(param, &s->info);
    out("{\"format\": {\"w\": %u, \"h\": %u, \"format\": %u}, \"key\": \"%s\"}", s->info.size.width,
        s->info.size.height, s->info.format, js(s->key));
    uint8_t buffer[1024];
    struct spa_pod_builder b = SPA_POD_BUILDER_INIT(buffer, sizeof(buffer));
    const struct spa_pod *params[2];
    params[0] = spa_pod_builder_add_object(&b, SPA_TYPE_OBJECT_ParamBuffers, SPA_PARAM_Buffers,
        SPA_PARAM_BUFFERS_buffers, SPA_POD_CHOICE_RANGE_Int(4, 2, 16),
        SPA_PARAM_BUFFERS_dataType, SPA_POD_CHOICE_FLAGS_Int((1 << SPA_DATA_MemFd) | (1 << SPA_DATA_MemPtr)));
    // The pointer's place as metadata. KWin sizes it for its own pointer picture; the range must reach that.
    params[1] = spa_pod_builder_add_object(&b, SPA_TYPE_OBJECT_ParamMeta, SPA_PARAM_Meta,
        SPA_PARAM_META_type, SPA_POD_Id(SPA_META_Cursor),
        SPA_PARAM_META_size, SPA_POD_CHOICE_RANGE_Int(CURSOR_META_SIZE(64, 64), CURSOR_META_SIZE(1, 1),
                                                      CURSOR_META_SIZE(1024, 1024)));
    pw_stream_update_params(s->pw, params, 2);
}

static void on_state_changed(void *data, enum pw_stream_state old, enum pw_stream_state state, const char *error) {
    struct stream *s = data;
    (void)old;
    out("{\"stream\": \"%s\", \"why\": \"%s\", \"key\": \"%s\"}", pw_stream_state_as_string(state), js(error),
        js(s->key));
}

static void write_cursor(uint32_t *h, struct stream *s) {
    ((int32_t *)h)[4] = s->cursor_x;
    ((int32_t *)h)[5] = s->cursor_y;
    h[6] = (uint32_t)s->cursor_seen;
}

static void on_process(void *data) {
    struct stream *s = data;
    struct pw_buffer *b = NULL, *n;
    while ((n = pw_stream_dequeue_buffer(s->pw)) != NULL) {  // the newest only
        if (b) pw_stream_queue_buffer(s->pw, b);
        b = n;
    }
    if (b == NULL) return;
    struct spa_buffer *buf = b->buffer;
    struct spa_meta_cursor *mc = spa_buffer_find_meta_data(buf, SPA_META_Cursor, sizeof(*mc));
    if (mc) {
        // KWin sends id 0 (not valid) when the pointer is off this picture: the window's or the monitor's.
        if (spa_meta_cursor_is_valid(mc)) {
            s->cursor_x = mc->position.x;
            s->cursor_y = mc->position.y;
            s->cursor_seen = 1;
        } else {
            s->cursor_seen = 0;
        }
    }
    struct spa_data *d = &buf->datas[0];
    if (getenv("PXR_KWIN_DEBUG")) {
        static int dn;
        if (dn++ < 8)
            out("{\"debug\": {\"key\": \"%s\", \"type\": %u, \"data\": %d, \"size\": %u, \"stride\": %d, "
                "\"maxsize\": %u, \"flags\": %d, \"cursor\": %d}}", js(s->key), d->type, d->data != NULL,
                d->chunk ? d->chunk->size : 0, d->chunk ? d->chunk->stride : 0, d->maxsize,
                d->chunk ? d->chunk->flags : 0, mc ? (int)mc->id : -1);
    }
    uint32_t *h = (uint32_t *)s->shm;
    int w = (int)s->info.size.width, ht = (int)s->info.size.height;
    uint32_t f = s->info.format;
    int bgr = f == SPA_VIDEO_FORMAT_BGRx || f == SPA_VIDEO_FORMAT_BGRA;
    int rgb = f == SPA_VIDEO_FORMAT_RGBx || f == SPA_VIDEO_FORMAT_RGBA;
    int picture = atomic_load(&s->pictures);
    if (h && picture && (bgr || rgb) && d->data != NULL && d->chunk != NULL && d->chunk->size > 0 &&
        !(d->chunk->flags & SPA_CHUNK_FLAG_CORRUPTED) && w > 0 && ht > 0 && (size_t)w * ht * 4 <= SHM_CAPACITY) {
        int stride = d->chunk->stride > 0 ? d->chunk->stride : w * 4;
        const uint8_t *src = (const uint8_t *)d->data + d->chunk->offset;
        if ((size_t)stride * (ht - 1) + (size_t)w * 4 + d->chunk->offset <= d->maxsize) {
            atomic_thread_fence(memory_order_seq_cst);
            h[1]++;  // odd: being written
            atomic_thread_fence(memory_order_seq_cst);
            h[2] = (uint32_t)w;
            h[3] = (uint32_t)ht;
            uint8_t *dst = s->shm + SHM_HEADER;
            for (int y = 0; y < ht; y++) {
                uint8_t *row = dst + (size_t)y * w * 4;
                const uint8_t *in = src + (size_t)y * stride;
                if (bgr) {
                    memcpy(row, in, (size_t)w * 4);
                } else {
                    for (int x = 0; x < w; x++) {  // RGBx to BGRx
                        row[x * 4 + 0] = in[x * 4 + 2];
                        row[x * 4 + 1] = in[x * 4 + 1];
                        row[x * 4 + 2] = in[x * 4 + 0];
                        row[x * 4 + 3] = in[x * 4 + 3];
                    }
                }
            }
            write_cursor(h, s);
            h[8] = ++s->frame_number;
            atomic_thread_fence(memory_order_seq_cst);
            h[1]++;  // even: done
        }
    } else if (h && mc) {
        // Only the pointer moved, or pictures are off: the picture stays, the pointer's place is updated.
        atomic_thread_fence(memory_order_seq_cst);
        h[1]++;
        atomic_thread_fence(memory_order_seq_cst);
        write_cursor(h, s);
        atomic_thread_fence(memory_order_seq_cst);
        h[1]++;
    }
    pw_stream_queue_buffer(s->pw, b);
}

static const struct pw_stream_events stream_events = {
    PW_VERSION_STREAM_EVENTS,
    .state_changed = on_state_changed,
    .param_changed = on_param_changed,
    .process = on_process,
};

static int pw_start(void) {
    if (pw_loop) return pw_core_ ? 0 : -1;
    pw_loop = pw_thread_loop_new("pxr-kwin", NULL);
    pw_ctx = pw_context_new(pw_thread_loop_get_loop(pw_loop), NULL, 0);
    pw_thread_loop_start(pw_loop);
    pw_thread_loop_lock(pw_loop);
    pw_core_ = pw_context_connect(pw_ctx, NULL, 0);
    pw_thread_loop_unlock(pw_loop);
    if (!pw_core_) out("{\"error\": \"pipewire: no connection to the PipeWire daemon\"}");
    return pw_core_ ? 0 : -1;
}

static void pw_connect_stream(struct stream *s) {
    if (pw_start() < 0) return;
    pw_thread_loop_lock(pw_loop);
    struct pw_properties *props = pw_properties_new(PW_KEY_MEDIA_TYPE, "Video", PW_KEY_MEDIA_CATEGORY, "Capture",
                                                    PW_KEY_MEDIA_ROLE, "Screen", NULL);
    if (s->serial) pw_properties_setf(props, PW_KEY_TARGET_OBJECT, "%" PRIu64, s->serial);
    s->pw = pw_stream_new(pw_core_, "Pocket XR", props);
    pw_stream_add_listener(s->pw, &s->hook, &stream_events, s);
    uint8_t buffer[1024];
    struct spa_pod_builder b = SPA_POD_BUILDER_INIT(buffer, sizeof(buffer));
    struct spa_rectangle def = SPA_RECTANGLE(1920, 1080), min = SPA_RECTANGLE(1, 1), max = SPA_RECTANGLE(8192, 8192);
    struct spa_fraction zero = SPA_FRACTION(0, 1), want = SPA_FRACTION(60, 1), most = SPA_FRACTION(1000, 1);
    const struct spa_pod *params[1];
    // No modifiers offered, so KWin sends shared memory frames rather than GPU buffers.
    params[0] = spa_pod_builder_add_object(&b, SPA_TYPE_OBJECT_Format, SPA_PARAM_EnumFormat,
        SPA_FORMAT_mediaType, SPA_POD_Id(SPA_MEDIA_TYPE_video),
        SPA_FORMAT_mediaSubtype, SPA_POD_Id(SPA_MEDIA_SUBTYPE_raw),
        SPA_FORMAT_VIDEO_format, SPA_POD_CHOICE_ENUM_Id(5, SPA_VIDEO_FORMAT_BGRx, SPA_VIDEO_FORMAT_BGRx,
                                                        SPA_VIDEO_FORMAT_BGRA, SPA_VIDEO_FORMAT_RGBx,
                                                        SPA_VIDEO_FORMAT_RGBA),
        SPA_FORMAT_VIDEO_size, SPA_POD_CHOICE_RANGE_Rectangle(&def, &min, &max),
        SPA_FORMAT_VIDEO_framerate, SPA_POD_Fraction(&zero),
        SPA_FORMAT_VIDEO_maxFramerate, SPA_POD_CHOICE_RANGE_Fraction(&want, &zero, &most));
    int r = pw_stream_connect(s->pw, PW_DIRECTION_INPUT, s->serial ? PW_ID_ANY : s->node,
                              PW_STREAM_FLAG_AUTOCONNECT | PW_STREAM_FLAG_MAP_BUFFERS, params, 1);
    pw_thread_loop_unlock(pw_loop);
    if (r < 0) out("{\"error\": \"pipewire: connect to node %u failed (%d)\", \"key\": \"%s\"}", s->node, r, js(s->key));
}

static void stream_free(struct stream *s) {
    if (pw_loop && s->pw) {
        pw_thread_loop_lock(pw_loop);
        pw_stream_disconnect(s->pw);
        pw_stream_destroy(s->pw);
        pw_thread_loop_unlock(pw_loop);
    }
    s->pw = NULL;
    if (s->k) zkde_screencast_stream_unstable_v1_close(s->k);
    s->k = NULL;
    if (s->shm) {
        munmap(s->shm, SHM_HEADER + SHM_CAPACITY);
        unlink(s->shm_path);
    }
    s->shm = NULL;
    s->used = 0;
}

// ---- KWin's stream object

static void kstream_closed(void *data, struct zkde_screencast_stream_unstable_v1 *k) {
    struct stream *s = data;
    (void)k;
    if (!s->used) return;
    out("{\"closed\": \"%s\"}", js(s->key));
    stream_free(s);
}
static void kstream_created(void *data, struct zkde_screencast_stream_unstable_v1 *k, uint32_t node) {
    struct stream *s = data;
    (void)k;
    if (!s->used || s->pw) return;
    s->node = node;
    if (shm_open_stream(s) < 0) {
        out("{\"failed\": \"%s\", \"error\": \"shared memory: %s\"}", js(s->key), js(strerror(errno)));
        stream_free(s);
        return;
    }
    pw_connect_stream(s);
    out("{\"shm\": \"%s\", \"key\": \"%s\", \"source\": \"%s\", \"node\": %u, \"serial\": %" PRIu64 "}",
        js(s->shm_path), js(s->key), js(s->source), node, s->serial);
}
static void kstream_failed(void *data, struct zkde_screencast_stream_unstable_v1 *k, const char *error) {
    struct stream *s = data;
    (void)k;
    if (!s->used) return;
    out("{\"failed\": \"%s\", \"error\": \"%s\"}", js(s->key), js(error));
    stream_free(s);
}
static void kstream_serial(void *data, struct zkde_screencast_stream_unstable_v1 *k, uint32_t hi, uint32_t lo) {
    struct stream *s = data;
    (void)k;
    s->serial = ((uint64_t)hi << 32) | lo;
}
static const struct zkde_screencast_stream_unstable_v1_listener kstream_listener = {
    kstream_closed, kstream_created, kstream_failed, kstream_serial};

static struct output *find_output(const char *connector) {
    struct output *o;
    wl_list_for_each(o, &outputs, link) if (strcmp(o->connector, connector) == 0) return o;
    return NULL;
}

static void record(const char *key, const char *kind, const char *what) {
    struct stream *old = find_stream(key);
    if (old) stream_free(old);
    if (!screencast) {
        out("{\"failed\": \"%s\", \"error\": \"KWin did not offer its screen cast to this program\"}", js(key));
        return;
    }
    struct stream *s = NULL;
    for (int i = 0; i < MAX_STREAMS && !s; i++)
        if (!streams[i].used) s = &streams[i];
    if (!s) {
        out("{\"failed\": \"%s\", \"error\": \"too many pictures at once\"}", js(key));
        return;
    }
    memset(s, 0, sizeof *s);
    s->used = 1;
    atomic_store(&s->pictures, 1);
    s->cursor_x = s->cursor_y = -1;
    snprintf(s->key, sizeof s->key, "%s", key);
    snprintf(s->source, sizeof s->source, "%s %s", kind, what);
    if (strcmp(kind, "output") == 0) {
        struct output *o = find_output(what);
        if (!o) {
            out("{\"failed\": \"%s\", \"error\": \"no monitor %s\"}", js(key), js(what));
            s->used = 0;
            return;
        }
        s->k = zkde_screencast_unstable_v1_stream_output(screencast, o->wl, POINTER_METADATA);
    } else if (strcmp(kind, "window") == 0) {
        s->k = zkde_screencast_unstable_v1_stream_window(screencast, what, POINTER_METADATA);
    } else if (strcmp(kind, "region") == 0) {
        int x, y, w, h;
        double scale = 1.0;
        if (screencast_version < 3 || sscanf(what, "%d %d %d %d %lf", &x, &y, &w, &h, &scale) < 4 || w <= 0 || h <= 0) {
            out("{\"failed\": \"%s\", \"error\": \"region: X Y W H [SCALE], KWin's screen cast version 3\"}", js(key));
            s->used = 0;
            return;
        }
        s->k = zkde_screencast_unstable_v1_stream_region(screencast, x, y, (uint32_t)w, (uint32_t)h,
                                                          wl_fixed_from_double(scale), POINTER_METADATA);
    } else {
        out("{\"failed\": \"%s\", \"error\": \"record what? (output or window)\"}", js(key));
        s->used = 0;
        return;
    }
    zkde_screencast_stream_unstable_v1_add_listener(s->k, &kstream_listener, s);
    wl_display_flush(dpy);
}

// ---------------------------------------------------------------- monitors

static void print_monitors(void) {
    char buf[16384];
    size_t n = (size_t)snprintf(buf, sizeof buf, "{\"monitors\": [");
    int first = 1;
    struct output *o;
    wl_list_for_each_reverse(o, &outputs, link) {
        if (!o->connector[0]) continue;
        int lw = o->lw > 0 ? o->lw : o->w / (o->scale > 0 ? o->scale : 1);
        double scale = lw > 0 ? (double)o->w / lw : 1.0;
        int x = o->xdg ? o->x : o->gx, y = o->xdg ? o->y : o->gy;
        n += (size_t)snprintf(buf + n, sizeof buf - n,
            "%s{\"connector\": \"%s\", \"vendor\": \"%s\", \"product\": \"%s\", \"name\": \"%s\", \"on\": true, "
            "\"x\": %d, \"y\": %d, \"w\": %d, \"h\": %d, \"lw\": %d, \"lh\": %d, \"rate\": %.3f, \"scale\": %.4f, "
            "\"primary\": false}",
            first ? "" : ", ", js(o->connector), js(o->make), js(o->model), js(o->description), x, y, o->w, o->h, lw,
            o->lh > 0 ? o->lh : o->h / (o->scale > 0 ? o->scale : 1), o->rate, scale);
        if (n >= sizeof buf) break;
        first = 0;
    }
    if (n < sizeof buf) out("%s]}", buf);
    monitors_dirty = 0;
}

static void wo_geometry(void *data, struct wl_output *w, int32_t x, int32_t y, int32_t pw, int32_t ph, int32_t sub,
                        const char *make, const char *model, int32_t transform) {
    struct output *o = data;
    (void)w; (void)pw; (void)ph; (void)sub; (void)transform;
    o->gx = x;
    o->gy = y;
    snprintf(o->make, sizeof o->make, "%s", make ? make : "");
    snprintf(o->model, sizeof o->model, "%s", model ? model : "");
}
static void wo_mode(void *data, struct wl_output *w, uint32_t flags, int32_t width, int32_t height, int32_t refresh) {
    struct output *o = data;
    (void)w;
    if (flags & WL_OUTPUT_MODE_CURRENT) {
        o->w = width;
        o->h = height;
        o->rate = refresh / 1000.0;
    }
}
static void wo_done(void *data, struct wl_output *w) {
    (void)data; (void)w;
    monitors_dirty = 1;
}
static void wo_scale(void *data, struct wl_output *w, int32_t factor) {
    struct output *o = data;
    (void)w;
    o->scale = factor;
}
static void wo_name(void *data, struct wl_output *w, const char *name) {
    struct output *o = data;
    (void)w;
    snprintf(o->connector, sizeof o->connector, "%s", name);
}
static void wo_description(void *data, struct wl_output *w, const char *d) {
    struct output *o = data;
    (void)w;
    snprintf(o->description, sizeof o->description, "%s", d);
}
static const struct wl_output_listener wo_listener = {wo_geometry, wo_mode, wo_done, wo_scale, wo_name, wo_description};

static void xo_position(void *data, struct zxdg_output_v1 *x, int32_t px, int32_t py) {
    struct output *o = data;
    (void)x;
    o->x = px;
    o->y = py;
}
static void xo_size(void *data, struct zxdg_output_v1 *x, int32_t w, int32_t h) {
    struct output *o = data;
    (void)x;
    o->lw = w;
    o->lh = h;
}
static void xo_done(void *data, struct zxdg_output_v1 *x) {
    (void)data; (void)x;
    monitors_dirty = 1;
}
static void xo_name(void *data, struct zxdg_output_v1 *x, const char *name) {
    struct output *o = data;
    (void)x;
    if (!o->connector[0]) snprintf(o->connector, sizeof o->connector, "%s", name);
}
static void xo_description(void *data, struct zxdg_output_v1 *x, const char *d) {
    struct output *o = data;
    (void)x;
    if (!o->description[0]) snprintf(o->description, sizeof o->description, "%s", d);
}
static const struct zxdg_output_v1_listener xo_listener = {xo_position, xo_size, xo_done, xo_name, xo_description};

static void output_add_xdg(struct output *o) {
    if (o->xdg || !xdg_outputs) return;
    o->xdg = zxdg_output_manager_v1_get_xdg_output(xdg_outputs, o->wl);
    zxdg_output_v1_add_listener(o->xdg, &xo_listener, o);
}

// ---------------------------------------------------------------- windows

static void print_windows(void) {
    size_t cap = 4096, n = 0;
    char *buf = malloc(cap);
    if (!buf) return;
#define ADD(...)                                                              \
    do {                                                                      \
        int need = snprintf(NULL, 0, __VA_ARGS__);                            \
        if (n + (size_t)need + 1 > cap) {                                     \
            cap = (n + (size_t)need + 1) * 2;                                 \
            char *nb = realloc(buf, cap);                                     \
            if (!nb) { free(buf); return; }                                   \
            buf = nb;                                                         \
        }                                                                     \
        n += (size_t)snprintf(buf + n, cap - n, __VA_ARGS__);                 \
    } while (0)
    ADD("{\"windows\": [");
    int first = 1;
    struct window *w;
    wl_list_for_each_reverse(w, &windows, link) {
        if (!w->ready || !w->uuid[0]) continue;
        ADD("%s{\"uuid\": \"%s\", \"title\": \"%s\", \"app_id\": \"%s\", \"resource\": \"%s\", \"pid\": %u, "
            "\"state\": %u, \"x\": %d, \"y\": %d, \"w\": %d, \"h\": %d}",
            first ? "" : ", ", js(w->uuid), js(w->title), js(w->app_id), js(w->resource), w->pid, w->state, w->x, w->y,
            w->w, w->h);
        first = 0;
    }
    ADD("], \"stacking\": [");
    if (stacking) {
        char *copy = strdup(stacking), *save = NULL;
        first = 1;
        for (char *t = strtok_r(copy, ";", &save); t; t = strtok_r(NULL, ";", &save)) {
            ADD("%s\"%s\"", first ? "" : ", ", js(t));
            first = 0;
        }
        free(copy);
    }
    ADD("]}");
#undef ADD
    out("%s", buf);
    free(buf);
    windows_dirty = 0;
}

static void pw_title(void *data, struct org_kde_plasma_window *p, const char *title) {
    struct window *w = data;
    (void)p;
    snprintf(w->title, sizeof w->title, "%s", title ? title : "");
    windows_dirty = 1;
}
static void pw_app_id(void *data, struct org_kde_plasma_window *p, const char *app_id) {
    struct window *w = data;
    (void)p;
    snprintf(w->app_id, sizeof w->app_id, "%s", app_id ? app_id : "");
    windows_dirty = 1;
}
static void pw_state(void *data, struct org_kde_plasma_window *p, uint32_t flags) {
    struct window *w = data;
    (void)p;
    w->state = flags;
    windows_dirty = 1;
}
static void pw_vd_changed(void *data, struct org_kde_plasma_window *p, int32_t number) {
    (void)data; (void)p; (void)number;
}
static void pw_icon_name(void *data, struct org_kde_plasma_window *p, const char *name) {
    (void)data; (void)p; (void)name;
}
static void pw_unmapped(void *data, struct org_kde_plasma_window *p) {
    struct window *w = data;
    wl_list_remove(&w->link);
    if (wm_version >= 4) org_kde_plasma_window_destroy(p);
    free(w);
    windows_dirty = 1;
}
static void pw_initial_state(void *data, struct org_kde_plasma_window *p) {
    struct window *w = data;
    (void)p;
    w->ready = 1;
    windows_dirty = 1;
}
static void pw_parent(void *data, struct org_kde_plasma_window *p, struct org_kde_plasma_window *parent) {
    (void)data; (void)p; (void)parent;
}
static void pw_geometry(void *data, struct org_kde_plasma_window *p, int32_t x, int32_t y, uint32_t width,
                        uint32_t height) {
    struct window *w = data;
    (void)p;
    w->x = x;
    w->y = y;
    w->w = (int)width;
    w->h = (int)height;
    windows_dirty = 1;
}
static void pw_icon_changed(void *data, struct org_kde_plasma_window *p) {
    (void)data; (void)p;
}
static void pw_pid(void *data, struct org_kde_plasma_window *p, uint32_t pid) {
    struct window *w = data;
    (void)p;
    w->pid = pid;
    windows_dirty = 1;
}
static void pw_vd_entered(void *data, struct org_kde_plasma_window *p, const char *id) {
    (void)data; (void)p; (void)id;
}
static void pw_vd_left(void *data, struct org_kde_plasma_window *p, const char *id) {
    (void)data; (void)p; (void)id;
}
static void pw_app_menu(void *data, struct org_kde_plasma_window *p, const char *service, const char *path) {
    (void)data; (void)p; (void)service; (void)path;
}
static void pw_activity_entered(void *data, struct org_kde_plasma_window *p, const char *id) {
    (void)data; (void)p; (void)id;
}
static void pw_activity_left(void *data, struct org_kde_plasma_window *p, const char *id) {
    (void)data; (void)p; (void)id;
}
static void pw_resource_name(void *data, struct org_kde_plasma_window *p, const char *name) {
    struct window *w = data;
    (void)p;
    snprintf(w->resource, sizeof w->resource, "%s", name ? name : "");
    windows_dirty = 1;
}
static const struct org_kde_plasma_window_listener pw_listener = {
    .title_changed = pw_title,
    .app_id_changed = pw_app_id,
    .state_changed = pw_state,
    .virtual_desktop_changed = pw_vd_changed,
    .themed_icon_name_changed = pw_icon_name,
    .unmapped = pw_unmapped,
    .initial_state = pw_initial_state,
    .parent_window = pw_parent,
    .geometry = pw_geometry,
    .icon_changed = pw_icon_changed,
    .pid_changed = pw_pid,
    .virtual_desktop_entered = pw_vd_entered,
    .virtual_desktop_left = pw_vd_left,
    .application_menu = pw_app_menu,
    .activity_entered = pw_activity_entered,
    .activity_left = pw_activity_left,
    .resource_name_changed = pw_resource_name,
};

static struct window *find_window(const char *uuid) {
    struct window *w;
    wl_list_for_each(w, &windows, link) if (strcmp(w->uuid, uuid) == 0) return w;
    return NULL;
}

static void wm_show_desktop(void *data, struct org_kde_plasma_window_management *m, uint32_t state) {
    (void)data; (void)m; (void)state;
}
static void wm_window(void *data, struct org_kde_plasma_window_management *m, uint32_t id) {
    // Older than window_with_uuid (version 13): no uuid, so no picture of it can be asked for. Not listed.
    (void)data; (void)m; (void)id;
}
static void wm_stacking(void *data, struct org_kde_plasma_window_management *m, struct wl_array *ids) {
    (void)data; (void)m; (void)ids;
}
static void wm_stacking_uuid(void *data, struct org_kde_plasma_window_management *m, const char *uuids) {
    (void)data; (void)m;
    free(stacking);
    stacking = strdup(uuids ? uuids : "");
    windows_dirty = 1;
}
static void wm_window_with_uuid(void *data, struct org_kde_plasma_window_management *m, uint32_t id, const char *uuid) {
    (void)data; (void)id;
    if (!uuid || find_window(uuid)) return;
    struct window *w = calloc(1, sizeof *w);
    if (!w) return;
    snprintf(w->uuid, sizeof w->uuid, "%s", uuid);
    w->pw = org_kde_plasma_window_management_get_window_by_uuid(m, uuid);
    org_kde_plasma_window_add_listener(w->pw, &pw_listener, w);
    wl_list_insert(&windows, &w->link);
}
static const struct org_kde_plasma_window_management_listener wm_listener = {
    .show_desktop_changed = wm_show_desktop,
    .window = wm_window,
    .stacking_order_changed = wm_stacking,
    .stacking_order_uuid_changed = wm_stacking_uuid,
    .window_with_uuid = wm_window_with_uuid,
};

// ---------------------------------------------------------------- registry

static uint32_t at_most(uint32_t a, uint32_t b) { return a < b ? a : b; }

static void registry_global(void *data, struct wl_registry *reg, uint32_t name, const char *iface, uint32_t version) {
    (void)data;
    if (strcmp(iface, zkde_screencast_unstable_v1_interface.name) == 0) {
        screencast_version = at_most(version, (uint32_t)zkde_screencast_unstable_v1_interface.version);
        screencast = wl_registry_bind(reg, name, &zkde_screencast_unstable_v1_interface, screencast_version);
    } else if (strcmp(iface, org_kde_plasma_window_management_interface.name) == 0) {
        wm_version = at_most(version, (uint32_t)org_kde_plasma_window_management_interface.version);
        if (wm_version >= 13) {
            wm = wl_registry_bind(reg, name, &org_kde_plasma_window_management_interface, wm_version);
            org_kde_plasma_window_management_add_listener(wm, &wm_listener, NULL);
        }
    } else if (strcmp(iface, org_kde_kwin_fake_input_interface.name) == 0) {
        fake_version = at_most(version, (uint32_t)org_kde_kwin_fake_input_interface.version);
        fake = wl_registry_bind(reg, name, &org_kde_kwin_fake_input_interface, fake_version);
        org_kde_kwin_fake_input_authenticate(fake, "Pocket XR",
                                             "Keeps the pointer on your screen while your glasses show it");
    } else if (strcmp(iface, zxdg_output_manager_v1_interface.name) == 0) {
        xdg_outputs = wl_registry_bind(reg, name, &zxdg_output_manager_v1_interface, at_most(version, 3));
        struct output *o;
        wl_list_for_each(o, &outputs, link) output_add_xdg(o);
    } else if (strcmp(iface, wl_output_interface.name) == 0) {
        struct output *o = calloc(1, sizeof *o);
        if (!o) return;
        o->name = name;
        o->scale = 1;
        o->wl = wl_registry_bind(reg, name, &wl_output_interface, at_most(version, 4));
        wl_output_add_listener(o->wl, &wo_listener, o);
        wl_list_insert(&outputs, &o->link);
        output_add_xdg(o);
    }
}

static void registry_remove(void *data, struct wl_registry *reg, uint32_t name) {
    (void)data; (void)reg;
    struct output *o, *t;
    wl_list_for_each_safe(o, t, &outputs, link) {
        if (o->name != name) continue;
        if (o->xdg) zxdg_output_v1_destroy(o->xdg);
        if (wl_output_get_version(o->wl) >= 3) wl_output_release(o->wl);
        else wl_output_destroy(o->wl);
        wl_list_remove(&o->link);
        free(o);
        monitors_dirty = 1;
    }
}
static const struct wl_registry_listener registry_listener = {registry_global, registry_remove};

// ---------------------------------------------------------------- commands

static int quitting;

static void command(char *line) {
    char a[200], b[64], c[200];
    double x, y;
    unsigned code;
    int down;
    size_t len = strlen(line);
    while (len && (line[len - 1] == '\n' || line[len - 1] == '\r' || line[len - 1] == ' ')) line[--len] = 0;
    if (sscanf(line, "record %199s %63s %199[^\n]", a, b, c) == 3) {
        record(a, b, c);
    } else if (sscanf(line, "stop %199s", a) == 1) {
        struct stream *s = find_stream(a);
        if (s) stream_free(s);
    } else if (sscanf(line, "pictures %199s %d", a, &down) == 2) {
        struct stream *s = find_stream(a);
        if (s) atomic_store(&s->pictures, down ? 1 : 0);
    } else if (sscanf(line, "move %lf %lf", &x, &y) == 2) {
        if (fake && fake_version >= 3)
            org_kde_kwin_fake_input_pointer_motion_absolute(fake, wl_fixed_from_double(x), wl_fixed_from_double(y));
    } else if (sscanf(line, "button %u %d", &code, &down) == 2) {
        if (fake) org_kde_kwin_fake_input_button(fake, code, down ? 1 : 0);
    } else if (sscanf(line, "key %u %d", &code, &down) == 2) {
        if (fake && fake_version >= 4) org_kde_kwin_fake_input_keyboard_key(fake, code, down ? 1 : 0);
    } else if (sscanf(line, "activate %199s", a) == 1) {
        struct window *w = find_window(a);
        if (w) {
            uint32_t flags = ORG_KDE_PLASMA_WINDOW_MANAGEMENT_STATE_ACTIVE | ORG_KDE_PLASMA_WINDOW_MANAGEMENT_STATE_MINIMIZED;
            org_kde_plasma_window_set_state(w->pw, flags, ORG_KDE_PLASMA_WINDOW_MANAGEMENT_STATE_ACTIVE);
        }
    } else if (sscanf(line, "close %199s", a) == 1) {
        struct window *w = find_window(a);
        if (w) org_kde_plasma_window_close(w->pw);
    } else if (strcmp(line, "monitors") == 0) {
        print_monitors();
    } else if (strcmp(line, "windows") == 0) {
        print_windows();
    } else if (strcmp(line, "quit") == 0) {
        quitting = 1;
    }
    wl_display_flush(dpy);
}

int main(void) {
    pw_init(NULL, NULL);
    wl_list_init(&outputs);
    wl_list_init(&windows);
    dpy = wl_display_connect(NULL);
    if (!dpy) {
        out("{\"error\": \"Wayland: no display (WAYLAND_DISPLAY=%s)\"}", js(getenv("WAYLAND_DISPLAY")));
        return 1;
    }
    registry = wl_display_get_registry(dpy);
    wl_registry_add_listener(registry, &registry_listener, NULL);
    wl_display_roundtrip(dpy);  // the globals
    wl_display_roundtrip(dpy);  // the outputs' and windows' first events
    wl_display_roundtrip(dpy);  // the windows' own first events
    out("{\"caps\": {\"screencast\": %u, \"windows\": %u, \"input\": %u, \"xdg_output\": %s}}", screencast_version,
        wm ? wm_version : 0, fake_version, xdg_outputs ? "true" : "false");
    if (!screencast)
        out("{\"error\": \"KWin did not offer its screen cast to this program: its desktop file with "
            "X-KDE-Wayland-Interfaces is missing (run install.sh again), or this is not KDE Plasma\"}");
    print_monitors();
    if (wm) print_windows();

    char line[1024];
    size_t have = 0;
    double last_windows = 0;
    int fd = wl_display_get_fd(dpy);
    while (!quitting) {
        while (wl_display_prepare_read(dpy) != 0) wl_display_dispatch_pending(dpy);
        wl_display_flush(dpy);
        struct pollfd p[2] = {{fd, POLLIN, 0}, {0, POLLIN, 0}};
        int r = poll(p, 2, 100);
        if (r > 0 && (p[0].revents & POLLIN)) {
            if (wl_display_read_events(dpy) < 0) {
                out("{\"error\": \"Wayland: the connection to KWin ended\"}");
                break;
            }
        } else {
            wl_display_cancel_read(dpy);
        }
        if (wl_display_dispatch_pending(dpy) < 0) {
            out("{\"error\": \"Wayland: %s\"}", js(strerror(wl_display_get_error(dpy))));
            break;
        }
        if (r > 0 && (p[1].revents & (POLLIN | POLLHUP))) {
            ssize_t got = read(0, line + have, sizeof line - 1 - have);
            if (got <= 0) break;  // the app went away
            have += (size_t)got;
            line[have] = 0;
            char *start = line, *nl;
            while ((nl = strchr(start, '\n')) != NULL) {
                *nl = 0;
                command(start);
                start = nl + 1;
            }
            have = strlen(start);
            memmove(line, start, have + 1);
            if (have == sizeof line - 1) have = 0;  // a line too long: dropped
        }
        if (monitors_dirty) print_monitors();
        // The window list at most 10 times a second: a window being dragged sends many geometry events.
        double t = now_s();
        if (windows_dirty && t - last_windows >= 0.1) {
            last_windows = t;
            print_windows();
        }
    }
    for (int i = 0; i < MAX_STREAMS; i++)
        if (streams[i].used) stream_free(&streams[i]);
    wl_display_roundtrip(dpy);
    if (pw_loop) pw_thread_loop_stop(pw_loop);
    wl_display_disconnect(dpy);
    return 0;
}
