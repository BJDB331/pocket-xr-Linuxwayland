// Pocket XR on GNOME (Wayland): the screen's picture and the pointer, through Mutter's own D-Bus interfaces.
//
// On a Wayland desktop an app cannot read the screen or move the pointer itself. GNOME's compositor, Mutter, offers
// both to programs on the user's session bus, with no dialog:
//   org.gnome.Mutter.DisplayConfig   the monitors (connector, maker, model, place, size, scale)
//   org.gnome.Mutter.ScreenCast      a monitor's picture as a PipeWire stream (the pointer as metadata, not drawn in)
//   org.gnome.Mutter.RemoteDesktop   pointer and keyboard input, linked to that screen cast
// These are the interfaces xdg-desktop-portal-gnome and gnome-remote-desktop use.
//
// The newest frame goes into a shared memory file the Python app maps (pocketxr/mutter.py), as BGRx with a small
// header guarded by a sequence number (odd while being written):
//   u32 magic 'PXRM', u32 seq, u32 width, u32 height, i32 cursor x, i32 cursor y, u32 cursor seen, u32 capacity,
//   u32 frame number (changes only with a new picture, not with a pointer move), then width*height*4 bytes from
//   offset 64.
//
// stdin, one command per line:
//   record CONNECTOR      start the picture of that monitor (stops any earlier one)
//   move X Y              pointer to (X, Y) in the recorded monitor's pixels
//   key KEYSYM 1|0        a key down or up (X keysym number)
//   button CODE 1|0       a pointer button down or up (evdev code, 272 left)
//   monitors              print the monitors again
//   quit
// stdout, one JSON object per line: {"monitors": [...]}, {"shm": path, "connector": ...}, {"stream": state},
// {"error": text}, {"input": true|false}.
// GPLv3.

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

#include <gio/gio.h>
#include <pipewire/pipewire.h>
#include <spa/buffer/meta.h>
#include <spa/param/video/format-utils.h>
#include <spa/param/buffers.h>
#include <spa/param/param.h>

#define SHM_HEADER 64
#define SHM_CAPACITY (64u * 1024u * 1024u)  // up to 4096x4096 BGRx
#define MAGIC 0x4D525850u                    // "PXRM"
// The cursor metadata with room for a w x h BGRA picture (as PipeWire's own examples size it).
#define CURSOR_META_SIZE(w, h) \
    ((int)(sizeof(struct spa_meta_cursor) + sizeof(struct spa_meta_bitmap) + (w) * (h) * 4))

static GDBusConnection *bus;
static GMainLoop *main_loop;

static char *rd_session;   // RemoteDesktop session object path
static char *sc_session;   // ScreenCast session object path
static char *sc_stream;    // ScreenCast stream object path
static guint stream_sub, closed_sub;
static int input_ok;

static char shm_path[128];
static uint8_t *shm;
static struct pw_thread_loop *pw_loop;
static struct pw_context *pw_ctx;
static struct pw_core *pw_core_;
static struct pw_stream *pw_stream_;
static struct spa_hook stream_hook;
static struct spa_video_info_raw video_info;
static int cursor_x = -1, cursor_y = -1, cursor_seen;
static uint32_t frame_number;

static void out(const char *fmt, ...) G_GNUC_PRINTF(1, 2);
static void out(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    vprintf(fmt, ap);
    va_end(ap);
    putchar('\n');
    fflush(stdout);
}

static void out_error(const char *what, GError *e) {
    gchar *msg = g_strescape(e ? e->message : "", NULL);
    out("{\"error\": \"%s: %s\"}", what, msg);
    g_free(msg);
}

// ---------------------------------------------------------------- monitors

static void print_monitors(void) {
    GError *e = NULL;
    GVariant *r = g_dbus_connection_call_sync(bus, "org.gnome.Mutter.DisplayConfig", "/org/gnome/Mutter/DisplayConfig",
                                              "org.gnome.Mutter.DisplayConfig", "GetCurrentState", NULL, NULL,
                                              G_DBUS_CALL_FLAGS_NONE, 5000, NULL, &e);
    if (!r) {
        out_error("DisplayConfig", e);
        g_clear_error(&e);
        return;
    }
    // (u serial, a((ssss)a(siiddada{sv})a{sv}) monitors, a(iiduba(ssss)a{sv}) logical monitors, a{sv} properties)
    guint32 serial;
    GVariantIter *mons, *logs;
    GVariant *props;
    g_variant_get(r, "(ua((ssss)a(siiddada{sv})a{sv})a(iiduba(ssss)a{sv})@a{sv})", &serial, &mons, &logs, &props);
    GString *s = g_string_new("{\"monitors\": [");
    // Logical monitors first: where each connector sits, its scale, and which is primary.
    GHashTable *place = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, g_free);
    gint32 lx, ly;
    gdouble lscale;
    guint32 ltransform;
    gboolean lprimary;
    GVariantIter *lmons;
    GVariant *lprops;
    g_variant_iter_free(logs);
    GVariant *logical = g_variant_get_child_value(r, 2);
    GVariantIter li;
    g_variant_iter_init(&li, logical);
    while (g_variant_iter_next(&li, "(iiduba(ssss)@a{sv})", &lx, &ly, &lscale, &ltransform, &lprimary, &lmons, &lprops)) {
        gchar *conn, *vendor, *product, *serialno;
        while (g_variant_iter_next(lmons, "(ssss)", &conn, &vendor, &product, &serialno)) {
            gchar *v = g_strdup_printf("%d %d %.4f %u %d", lx, ly, lscale, ltransform, lprimary ? 1 : 0);
            g_hash_table_insert(place, conn, v);
            g_free(vendor);
            g_free(product);
            g_free(serialno);
        }
        g_variant_iter_free(lmons);
        g_variant_unref(lprops);
    }
    g_variant_unref(logical);
    gchar *conn, *vendor, *product, *serialno;
    GVariantIter *modes;
    GVariant *mprops;
    int first = 1;
    while (g_variant_iter_next(mons, "((ssss)a(siiddada{sv})@a{sv})", &conn, &vendor, &product, &serialno, &modes, &mprops)) {
        int w = 0, h = 0;
        double rate = 0;
        gchar *mid;
        gint32 mw, mh;
        gdouble mrate, mpref;
        GVariantIter *scales;
        GVariant *modeprops;
        while (g_variant_iter_next(modes, "(siidd@ad@a{sv})", &mid, &mw, &mh, &mrate, &mpref, NULL, &modeprops)) {
            gboolean current = FALSE;
            g_variant_lookup(modeprops, "is-current", "b", &current);
            if (current) {
                w = mw;
                h = mh;
                rate = mrate;
            }
            g_free(mid);
            g_variant_unref(modeprops);
        }
        (void)scales;
        g_variant_iter_free(modes);
        gchar *display_name = NULL;
        g_variant_lookup(mprops, "display-name", "s", &display_name);
        const char *pl = g_hash_table_lookup(place, conn);
        int x = 0, y = 0, primary = 0, on = pl != NULL;
        unsigned transform = 0;
        double scale = 1.0;
        if (pl) sscanf(pl, "%d %d %lf %u %d", &x, &y, &scale, &transform, &primary);
        gchar *ec = g_strescape(conn, NULL), *ev = g_strescape(vendor, NULL), *ep = g_strescape(product, NULL),
              *ed = g_strescape(display_name ? display_name : "", NULL);
        g_string_append_printf(s,
            "%s{\"connector\": \"%s\", \"vendor\": \"%s\", \"product\": \"%s\", \"name\": \"%s\", \"on\": %s, "
            "\"x\": %d, \"y\": %d, \"w\": %d, \"h\": %d, \"rate\": %.3f, \"scale\": %.4f, \"transform\": %u, "
            "\"primary\": %s}",
            first ? "" : ", ", ec, ev, ep, ed, on ? "true" : "false", x, y, w, h, rate, scale, transform,
            primary ? "true" : "false");
        first = 0;
        g_free(ec); g_free(ev); g_free(ep); g_free(ed);
        g_free(display_name);
        g_free(conn); g_free(vendor); g_free(product); g_free(serialno);
        g_variant_unref(mprops);
    }
    g_variant_iter_free(mons);
    g_variant_unref(props);
    g_hash_table_destroy(place);
    // Logical layout: "physical" (1) or "logical" (2) pixels for the places above.
    guint32 layout = 0;
    g_variant_lookup(props, "layout-mode", "u", &layout);
    g_string_append_printf(s, "], \"layout_mode\": %u}", layout);
    out("%s", s->str);
    g_string_free(s, TRUE);
    g_variant_unref(r);
}

static void on_monitors_changed(GDBusConnection *c, const gchar *sender, const gchar *path, const gchar *iface,
                                const gchar *signal, GVariant *params, gpointer data) {
    (void)c; (void)sender; (void)path; (void)iface; (void)signal; (void)params; (void)data;
    print_monitors();
}

// ---------------------------------------------------------------- shared memory

static int shm_open_file(void) {
    if (shm) return 0;
    snprintf(shm_path, sizeof shm_path, "/dev/shm/pxr-mutter-%d", (int)getpid());
    int fd = open(shm_path, O_RDWR | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
    if (fd < 0) return -1;
    if (ftruncate(fd, SHM_HEADER + SHM_CAPACITY) < 0) {
        close(fd);
        return -1;
    }
    shm = mmap(NULL, SHM_HEADER + SHM_CAPACITY, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    close(fd);
    if (shm == MAP_FAILED) {
        shm = NULL;
        return -1;
    }
    uint32_t *h = (uint32_t *)shm;
    h[0] = MAGIC;
    h[1] = 0;
    h[7] = SHM_CAPACITY;
    return 0;
}

// ---------------------------------------------------------------- PipeWire

static void on_param_changed(void *data, uint32_t id, const struct spa_pod *param) {
    (void)data;
    if (param == NULL || id != SPA_PARAM_Format) return;
    uint32_t media_type, media_subtype;
    if (spa_format_parse(param, &media_type, &media_subtype) < 0) return;
    if (media_type != SPA_MEDIA_TYPE_video || media_subtype != SPA_MEDIA_SUBTYPE_raw) return;
    spa_format_video_raw_parse(param, &video_info);
    out("{\"format\": {\"w\": %u, \"h\": %u, \"format\": %u, \"fps\": \"%u/%u\"}}", video_info.size.width,
        video_info.size.height, video_info.format, video_info.max_framerate.num, video_info.max_framerate.denom);
    uint8_t buffer[1024];
    struct spa_pod_builder b = SPA_POD_BUILDER_INIT(buffer, sizeof(buffer));
    const struct spa_pod *params[2];
    params[0] = spa_pod_builder_add_object(&b, SPA_TYPE_OBJECT_ParamBuffers, SPA_PARAM_Buffers,
        SPA_PARAM_BUFFERS_buffers, SPA_POD_CHOICE_RANGE_Int(4, 2, 16),
        SPA_PARAM_BUFFERS_dataType, SPA_POD_CHOICE_FLAGS_Int((1 << SPA_DATA_MemFd) | (1 << SPA_DATA_MemPtr)));
    // The pointer as metadata (its place, and its picture, which is not used). Mutter asks a fixed size (room for a
    // 384x384 pointer in Mutter 46), so the range must reach it or the metadata is left out.
    params[1] = spa_pod_builder_add_object(&b, SPA_TYPE_OBJECT_ParamMeta, SPA_PARAM_Meta,
        SPA_PARAM_META_type, SPA_POD_Id(SPA_META_Cursor),
        SPA_PARAM_META_size, SPA_POD_CHOICE_RANGE_Int(CURSOR_META_SIZE(64, 64), CURSOR_META_SIZE(1, 1),
                                                      CURSOR_META_SIZE(1024, 1024)));
    pw_stream_update_params(pw_stream_, params, 2);
}

static void on_state_changed(void *data, enum pw_stream_state old, enum pw_stream_state state, const char *error) {
    (void)data; (void)old;
    gchar *e = g_strescape(error ? error : "", NULL);
    out("{\"stream\": \"%s\", \"why\": \"%s\"}", pw_stream_state_as_string(state), e);
    g_free(e);
}

static void on_process(void *data) {
    (void)data;
    struct pw_buffer *b = NULL, *n;
    while ((n = pw_stream_dequeue_buffer(pw_stream_)) != NULL) {  // the newest only
        if (b) pw_stream_queue_buffer(pw_stream_, b);
        b = n;
    }
    if (b == NULL) return;
    struct spa_buffer *buf = b->buffer;
    struct spa_meta_cursor *mc = spa_buffer_find_meta_data(buf, SPA_META_Cursor, sizeof(*mc));
    if (getenv("PXR_MUTTER_DEBUG") && mc) {
        static int n;
        if (n++ < 20) out("{\"cursor_meta\": {\"id\": %u, \"x\": %d, \"y\": %d}}", mc->id, mc->position.x, mc->position.y);
    }
    if (mc && spa_meta_cursor_is_valid(mc)) {
        cursor_x = mc->position.x;
        cursor_y = mc->position.y;
        cursor_seen = 1;
    }
    struct spa_data *d = &buf->datas[0];
    uint32_t *h = (uint32_t *)shm;
    int w = (int)video_info.size.width, ht = (int)video_info.size.height;
    int fmt_ok = video_info.format == SPA_VIDEO_FORMAT_BGRx || video_info.format == SPA_VIDEO_FORMAT_BGRA;
    if (h && fmt_ok && d->data != NULL && d->chunk != NULL && d->chunk->size > 0 &&
        !(d->chunk->flags & SPA_CHUNK_FLAG_CORRUPTED) && w > 0 && ht > 0 && (size_t)w * ht * 4 <= SHM_CAPACITY) {
        int stride = d->chunk->stride > 0 ? d->chunk->stride : w * 4;
        const uint8_t *src = (const uint8_t *)d->data + d->chunk->offset;
        if ((size_t)stride * (ht - 1) + (size_t)w * 4 <= d->maxsize) {
            atomic_thread_fence(memory_order_seq_cst);
            h[1]++;  // odd: being written
            atomic_thread_fence(memory_order_seq_cst);
            h[2] = (uint32_t)w;
            h[3] = (uint32_t)ht;
            for (int y = 0; y < ht; y++) memcpy(shm + SHM_HEADER + (size_t)y * w * 4, src + (size_t)y * stride, (size_t)w * 4);
            ((int32_t *)h)[4] = cursor_x;
            ((int32_t *)h)[5] = cursor_y;
            h[6] = (uint32_t)cursor_seen;
            h[8] = ++frame_number;
            atomic_thread_fence(memory_order_seq_cst);
            h[1]++;  // even: done
        }
    } else if (h && mc && spa_meta_cursor_is_valid(mc)) {
        // A buffer with only the pointer moved: the picture stays, its place is updated.
        atomic_thread_fence(memory_order_seq_cst);
        h[1]++;
        atomic_thread_fence(memory_order_seq_cst);
        ((int32_t *)h)[4] = cursor_x;
        ((int32_t *)h)[5] = cursor_y;
        h[6] = (uint32_t)cursor_seen;
        atomic_thread_fence(memory_order_seq_cst);
        h[1]++;
    }
    pw_stream_queue_buffer(pw_stream_, b);
}

static const struct pw_stream_events stream_events = {
    PW_VERSION_STREAM_EVENTS,
    .state_changed = on_state_changed,
    .param_changed = on_param_changed,
    .process = on_process,
};

static void pw_connect_node(uint32_t node) {
    if (!pw_loop) {
        pw_loop = pw_thread_loop_new("pxr-mutter", NULL);
        pw_ctx = pw_context_new(pw_thread_loop_get_loop(pw_loop), NULL, 0);
        pw_thread_loop_start(pw_loop);
    }
    pw_thread_loop_lock(pw_loop);
    if (!pw_core_) pw_core_ = pw_context_connect(pw_ctx, NULL, 0);
    if (!pw_core_) {
        pw_thread_loop_unlock(pw_loop);
        out("{\"error\": \"pipewire: no connection to the PipeWire daemon\"}");
        return;
    }
    pw_stream_ = pw_stream_new(pw_core_, "Pocket XR",
                               pw_properties_new(PW_KEY_MEDIA_TYPE, "Video", PW_KEY_MEDIA_CATEGORY, "Capture",
                                                 PW_KEY_MEDIA_ROLE, "Screen", NULL));
    pw_stream_add_listener(pw_stream_, &stream_hook, &stream_events, NULL);
    uint8_t buffer[1024];
    struct spa_pod_builder b = SPA_POD_BUILDER_INIT(buffer, sizeof(buffer));
    struct spa_rectangle def = SPA_RECTANGLE(1920, 1080), min = SPA_RECTANGLE(1, 1), max = SPA_RECTANGLE(8192, 8192);
    struct spa_fraction zero = SPA_FRACTION(0, 1), want = SPA_FRACTION(60, 1), most = SPA_FRACTION(1000, 1);
    const struct spa_pod *params[1];
    // No modifiers offered, so Mutter sends shared memory frames rather than GPU buffers.
    params[0] = spa_pod_builder_add_object(&b, SPA_TYPE_OBJECT_Format, SPA_PARAM_EnumFormat,
        SPA_FORMAT_mediaType, SPA_POD_Id(SPA_MEDIA_TYPE_video),
        SPA_FORMAT_mediaSubtype, SPA_POD_Id(SPA_MEDIA_SUBTYPE_raw),
        SPA_FORMAT_VIDEO_format, SPA_POD_CHOICE_ENUM_Id(3, SPA_VIDEO_FORMAT_BGRx, SPA_VIDEO_FORMAT_BGRx, SPA_VIDEO_FORMAT_BGRA),
        SPA_FORMAT_VIDEO_size, SPA_POD_CHOICE_RANGE_Rectangle(&def, &min, &max),
        SPA_FORMAT_VIDEO_framerate, SPA_POD_Fraction(&zero),
        SPA_FORMAT_VIDEO_maxFramerate, SPA_POD_CHOICE_RANGE_Fraction(&want, &zero, &most));
    int r = pw_stream_connect(pw_stream_, PW_DIRECTION_INPUT, node, PW_STREAM_FLAG_AUTOCONNECT | PW_STREAM_FLAG_MAP_BUFFERS,
                              params, 1);
    pw_thread_loop_unlock(pw_loop);
    if (r < 0) out("{\"error\": \"pipewire: connect to node %u failed (%d)\"}", node, r);
}

static void pw_disconnect(void) {
    if (!pw_loop) return;
    pw_thread_loop_lock(pw_loop);
    if (pw_stream_) {
        pw_stream_disconnect(pw_stream_);
        pw_stream_destroy(pw_stream_);
        pw_stream_ = NULL;
    }
    pw_thread_loop_unlock(pw_loop);
}

// ---------------------------------------------------------------- Mutter sessions

static GVariant *call(const char *dest, const char *path, const char *iface, const char *method, GVariant *args,
                      const char *reply, GError **e) {
    return g_dbus_connection_call_sync(bus, dest, path, iface, method, args, reply ? G_VARIANT_TYPE(reply) : NULL,
                                       G_DBUS_CALL_FLAGS_NONE, 10000, NULL, e);
}

static void stop_sessions(void) {
    pw_disconnect();
    if (stream_sub) g_dbus_connection_signal_unsubscribe(bus, stream_sub);
    if (closed_sub) g_dbus_connection_signal_unsubscribe(bus, closed_sub);
    stream_sub = closed_sub = 0;
    GError *e = NULL;
    if (rd_session) {
        GVariant *r = call("org.gnome.Mutter.RemoteDesktop", rd_session, "org.gnome.Mutter.RemoteDesktop.Session", "Stop",
                           NULL, NULL, &e);
        if (r) g_variant_unref(r);
        g_clear_error(&e);
    } else if (sc_session) {
        GVariant *r = call("org.gnome.Mutter.ScreenCast", sc_session, "org.gnome.Mutter.ScreenCast.Session", "Stop",
                           NULL, NULL, &e);
        if (r) g_variant_unref(r);
        g_clear_error(&e);
    }
    g_clear_pointer(&rd_session, g_free);
    g_clear_pointer(&sc_session, g_free);
    g_clear_pointer(&sc_stream, g_free);
    input_ok = 0;
}

static void on_stream_added(GDBusConnection *c, const gchar *sender, const gchar *path, const gchar *iface,
                            const gchar *signal, GVariant *params, gpointer data) {
    (void)c; (void)sender; (void)path; (void)iface; (void)signal; (void)data;
    guint32 node;
    g_variant_get(params, "(u)", &node);
    out("{\"node\": %u}", node);
    pw_connect_node(node);
}

static void on_closed(GDBusConnection *c, const gchar *sender, const gchar *path, const gchar *iface,
                      const gchar *signal, GVariant *params, gpointer data) {
    (void)c; (void)sender; (void)iface; (void)signal; (void)params; (void)data;
    out("{\"closed\": \"%s\"}", path);
    g_clear_pointer(&rd_session, g_free);
    g_clear_pointer(&sc_session, g_free);
    pw_disconnect();
}

static void record(const char *connector) {
    stop_sessions();
    GError *e = NULL;
    GVariant *r;
    // Input first: a screen cast linked to a remote desktop session gives pointer moves in that stream's pixels.
    r = call("org.gnome.Mutter.RemoteDesktop", "/org/gnome/Mutter/RemoteDesktop", "org.gnome.Mutter.RemoteDesktop",
             "CreateSession", NULL, "(o)", &e);
    gchar *rd_id = NULL;
    if (r) {
        g_variant_get(r, "(o)", &rd_session);
        g_variant_unref(r);
        r = g_dbus_connection_call_sync(bus, "org.gnome.Mutter.RemoteDesktop", rd_session,
                                        "org.freedesktop.DBus.Properties", "Get",
                                        g_variant_new("(ss)", "org.gnome.Mutter.RemoteDesktop.Session", "SessionId"),
                                        G_VARIANT_TYPE("(v)"), G_DBUS_CALL_FLAGS_NONE, 5000, NULL, &e);
        if (r) {
            GVariant *v;
            g_variant_get(r, "(v)", &v);
            rd_id = g_variant_dup_string(v, NULL);
            g_variant_unref(v);
            g_variant_unref(r);
        } else {
            out_error("RemoteDesktop SessionId", e);
            g_clear_error(&e);
            g_clear_pointer(&rd_session, g_free);
        }
    } else {
        out_error("RemoteDesktop (no pointer moves)", e);
        g_clear_error(&e);
    }
    GVariantBuilder opts;
    g_variant_builder_init(&opts, G_VARIANT_TYPE("a{sv}"));
    if (rd_id) g_variant_builder_add(&opts, "{sv}", "remote-desktop-session-id", g_variant_new_string(rd_id));
    g_free(rd_id);
    r = call("org.gnome.Mutter.ScreenCast", "/org/gnome/Mutter/ScreenCast", "org.gnome.Mutter.ScreenCast",
             "CreateSession", g_variant_new("(a{sv})", &opts), "(o)", &e);
    if (!r) {
        out_error("ScreenCast", e);
        g_clear_error(&e);
        stop_sessions();
        return;
    }
    g_variant_get(r, "(o)", &sc_session);
    g_variant_unref(r);
    GVariantBuilder props;
    g_variant_builder_init(&props, G_VARIANT_TYPE("a{sv}"));
    g_variant_builder_add(&props, "{sv}", "cursor-mode", g_variant_new_uint32(2));  // metadata: drawn by the app
    r = call("org.gnome.Mutter.ScreenCast", sc_session, "org.gnome.Mutter.ScreenCast.Session", "RecordMonitor",
             g_variant_new("(sa{sv})", connector, &props), "(o)", &e);
    if (!r) {
        out_error("RecordMonitor", e);
        g_clear_error(&e);
        stop_sessions();
        return;
    }
    g_variant_get(r, "(o)", &sc_stream);
    g_variant_unref(r);
    stream_sub = g_dbus_connection_signal_subscribe(bus, "org.gnome.Mutter.ScreenCast", "org.gnome.Mutter.ScreenCast.Stream",
                                                    "PipeWireStreamAdded", sc_stream, NULL, G_DBUS_SIGNAL_FLAGS_NONE,
                                                    on_stream_added, NULL, NULL);
    closed_sub = g_dbus_connection_signal_subscribe(bus, NULL,
                                                    rd_session ? "org.gnome.Mutter.RemoteDesktop.Session"
                                                               : "org.gnome.Mutter.ScreenCast.Session",
                                                    "Closed", rd_session ? rd_session : sc_session, NULL,
                                                    G_DBUS_SIGNAL_FLAGS_NONE, on_closed, NULL, NULL);
    if (rd_session)
        r = call("org.gnome.Mutter.RemoteDesktop", rd_session, "org.gnome.Mutter.RemoteDesktop.Session", "Start", NULL,
                 NULL, &e);
    else
        r = call("org.gnome.Mutter.ScreenCast", sc_session, "org.gnome.Mutter.ScreenCast.Session", "Start", NULL, NULL, &e);
    if (!r) {
        out_error("Start", e);
        g_clear_error(&e);
        stop_sessions();
        return;
    }
    g_variant_unref(r);
    input_ok = rd_session != NULL;
    gchar *ec = g_strescape(connector, NULL);
    out("{\"shm\": \"%s\", \"connector\": \"%s\", \"input\": %s}", shm_path, ec, input_ok ? "true" : "false");
    g_free(ec);
}

static void rd_done(GObject *src, GAsyncResult *res, gpointer data) {
    GError *e = NULL;
    GVariant *r = g_dbus_connection_call_finish(G_DBUS_CONNECTION(src), res, &e);
    if (r) {
        g_variant_unref(r);
    } else {
        static int said;
        if (said++ < 5) out_error((const char *)data, e);  // a few, not one per pointer move
        g_clear_error(&e);
    }
}

static void rd_call(const char *method, GVariant *args) {
    if (!input_ok) {
        g_variant_unref(g_variant_ref_sink(args));
        return;
    }
    // Fire and forget: input must not wait on the bus.
    g_dbus_connection_call(bus, "org.gnome.Mutter.RemoteDesktop", rd_session, "org.gnome.Mutter.RemoteDesktop.Session",
                           method, args, NULL, G_DBUS_CALL_FLAGS_NONE, 2000, NULL, rd_done, (gpointer)method);
}

// ---------------------------------------------------------------- commands

static gboolean on_stdin(GIOChannel *ch, GIOCondition cond, gpointer data) {
    (void)data;
    if (cond & (G_IO_HUP | G_IO_ERR)) {
        g_main_loop_quit(main_loop);
        return FALSE;
    }
    gchar *line = NULL;
    gsize len = 0;
    GIOStatus st = g_io_channel_read_line(ch, &line, &len, NULL, NULL);
    if (st == G_IO_STATUS_EOF) {
        g_main_loop_quit(main_loop);
        return FALSE;
    }
    if (st != G_IO_STATUS_NORMAL || !line) return TRUE;
    g_strstrip(line);
    char arg[256];
    double x, y;
    unsigned code;
    int down;
    if (sscanf(line, "record %255s", arg) == 1) {
        record(arg);
    } else if (sscanf(line, "move %lf %lf", &x, &y) == 2) {
        if (sc_stream) rd_call("NotifyPointerMotionAbsolute", g_variant_new("(sdd)", sc_stream, x, y));
    } else if (sscanf(line, "key %u %d", &code, &down) == 2) {
        rd_call("NotifyKeyboardKeysym", g_variant_new("(ub)", code, down ? TRUE : FALSE));
    } else if (sscanf(line, "button %u %d", &code, &down) == 2) {
        rd_call("NotifyPointerButton", g_variant_new("(ib)", (gint32)code, down ? TRUE : FALSE));
    } else if (strcmp(line, "monitors") == 0) {
        print_monitors();
    } else if (strcmp(line, "stop") == 0) {
        stop_sessions();
    } else if (strcmp(line, "quit") == 0) {
        g_main_loop_quit(main_loop);
    }
    g_free(line);
    return TRUE;
}

int main(void) {
    GError *e = NULL;
    pw_init(NULL, NULL);
    bus = g_bus_get_sync(G_BUS_TYPE_SESSION, NULL, &e);
    if (!bus) {
        out_error("session bus", e);
        return 1;
    }
    if (shm_open_file() < 0) {
        out("{\"error\": \"shared memory: %s\"}", strerror(errno));
        return 1;
    }
    g_dbus_connection_signal_subscribe(bus, "org.gnome.Mutter.DisplayConfig", "org.gnome.Mutter.DisplayConfig",
                                       "MonitorsChanged", "/org/gnome/Mutter/DisplayConfig", NULL,
                                       G_DBUS_SIGNAL_FLAGS_NONE, on_monitors_changed, NULL, NULL);
    print_monitors();
    main_loop = g_main_loop_new(NULL, FALSE);
    GIOChannel *in = g_io_channel_unix_new(0);
    g_io_add_watch(in, G_IO_IN | G_IO_HUP | G_IO_ERR, on_stdin, NULL);
    g_main_loop_run(main_loop);
    stop_sessions();
    if (pw_loop) {
        pw_thread_loop_stop(pw_loop);
    }
    unlink(shm_path);
    return 0;
}
