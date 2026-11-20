// Pocket XR on the Steam Deck as a streaming host: one PC screen's picture, from a new KWin virtual output to the
// phone. The Python host (pocketxr/host) starts one of these per stream, after the phone's PLAY.
//
//   KWin (zkde_screencast_unstable_v1 stream_virtual_output, the pointer drawn in) -> PipeWire (shared memory frames)
//   -> FFmpeg: hwupload + scale_vaapi to NV12 -> h264_vaapi / hevc_vaapi, peak-limited variable bitrate, no B-frames,
//   a key frame only when asked -> Moonlight's video packets (RTP, NV_VIDEO_PACKET, Reed-Solomon FEC through nanors)
//   -> UDP to the address the phone's pings come from.
//
// The packet layout follows Sunshine's stream.cpp videoBroadcastThread (GPLv3), as Pocket XR for Windows' HostVideo.
// KWin lets a program use this protocol only when a desktop file names it (X-KDE-Wayland-Interfaces), which the
// installer writes for this program.
//
// Usage: pxr-host-video --width W --height H --fps F --kbps K --codec h264|hevc --port P --packet-size N
//                       --min-fec M [--name NAME] [--description TEXT] [--seconds S]
//        pxr-host-video --probe      (prints "codecs h264 hevc ..." for the encoders that open here)
// stdin: "idr" (a key frame next), "quit". stdout: one line per event ("output ...", "client ...", "stats ...",
// "error ..."). GPLv3.

#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <inttypes.h>
#include <netinet/in.h>
#include <poll.h>
#include <stdarg.h>
#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#include <wayland-client.h>
#include "zkde-screencast-unstable-v1-client-protocol.h"

#include <pipewire/pipewire.h>
#include <spa/param/video/format-utils.h>
#include <spa/param/buffers.h>
#include <spa/debug/types.h>

#include <libavcodec/avcodec.h>
#include <libavfilter/avfilter.h>
#include <libavfilter/buffersink.h>
#include <libavfilter/buffersrc.h>
#include <libavutil/hwcontext.h>
#include <libavutil/opt.h>

#include "rs.h"

// ---- settings ----

static int W = 1920, H = 1080, FPS = 60, KBPS = 20000, PORT = 0, PACKET_SIZE = 1392, MIN_FEC = 2, FEC_PERCENT = 20;
static const char *CODEC = "h264";
static const char *NAME = "pocketxr";
static const char *DESCRIPTION = "Pocket XR PC screen";
static double SECONDS = 0;
static const char *DEVICE = "/dev/dri/renderD128";

static atomic_int quitting;
static atomic_int idr_wanted = 1;

static double now_s(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec / 1e9;
}

static void say(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void say(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    vprintf(fmt, ap);
    va_end(ap);
    putchar('\n');
    fflush(stdout);
}

// ---- the video stream to the phone ----

static int sock = -1;
static pthread_mutex_t client_lock = PTHREAD_MUTEX_INITIALIZER;
static struct sockaddr_in client;
static int have_client;
static uint32_t low_seq, frame_index;
static double epoch;
static long long frames_sent, packets_sent, bytes_sent;

static void *pings(void *arg) {
    (void)arg;
    uint8_t buf[256];
    while (!quitting) {
        struct sockaddr_in from;
        socklen_t fl = sizeof(from);
        ssize_t n = recvfrom(sock, buf, sizeof(buf), 0, (struct sockaddr *)&from, &fl);
        if (n < 0) {
            if (errno == EINTR || errno == EAGAIN) continue;
            if (quitting) break;
            continue;
        }
        if (n < 4) continue;
        pthread_mutex_lock(&client_lock);
        int first = !have_client;
        client = from;
        have_client = 1;
        pthread_mutex_unlock(&client_lock);
        if (first) {
            char ip[64];
            inet_ntop(AF_INET, &from.sin_addr, ip, sizeof(ip));
            say("client %s:%d", ip, ntohs(from.sin_port));
            idr_wanted = 1;  // the phone's first picture is a key frame
        }
    }
    return NULL;
}

static void put16le(uint8_t *p, uint32_t v) { p[0] = v; p[1] = v >> 8; }
static void put32le(uint8_t *p, uint32_t v) { p[0] = v; p[1] = v >> 8; p[2] = v >> 16; p[3] = v >> 24; }
static void put16be(uint8_t *p, uint32_t v) { p[0] = v >> 8; p[1] = v; }
static void put32be(uint8_t *p, uint32_t v) { p[0] = v >> 24; p[1] = v >> 16; p[2] = v >> 8; p[3] = v; }

#define RTP_HEADER 16  // 12 bytes and 4 reserved
#define HEADER_SIZE (RTP_HEADER + 16)
#define MAX_FEC_BLOCKS 4

static void send_block(const struct sockaddr_in *to, const uint8_t *block, int len, int block_index, int blocks,
                       int fec, uint32_t timestamp) {
    int bs = PACKET_SIZE + RTP_HEADER;
    int data_shards = (len + bs - 1) / bs;
    int parity = (data_shards * fec + 99) / 100;
    if (parity < MIN_FEC && fec != 0) {
        parity = MIN_FEC;
        fec = 100 * parity / data_shards;
    }
    if (data_shards + parity > 255) parity = 255 - data_shards;
    if (parity < 0) parity = 0;
    int total = data_shards + parity;
    uint8_t *shards = calloc((size_t)total, (size_t)bs);
    if (!shards) return;
    memcpy(shards, block, (size_t)len);
    uint8_t multi = (uint8_t)((block_index << 4) | ((blocks - 1) << 6));
    for (int x = 0; x < data_shards; x++) {
        uint8_t *h = shards + (size_t)x * bs;
        put32le(h + 16, (low_seq + (uint32_t)x) << 8);
        put32le(h + 20, frame_index);
        uint8_t flags = 0x1;                     // picture data
        if (x == 0) flags |= 0x4;                // start of frame
        if (x == data_shards - 1) flags |= 0x2;  // end of frame
        h[24] = flags;
        h[26] = 0x10;
        h[27] = multi;
    }
    if (fec != 0 && parity > 0) {
        uint8_t **p = malloc(sizeof(uint8_t *) * (size_t)total);
        reed_solomon *rs = reed_solomon_new(data_shards, parity);
        if (p && rs) {
            for (int i = 0; i < total; i++) p[i] = shards + (size_t)i * bs;
            if (reed_solomon_encode(rs, p, total, bs) != 0) say("error FEC encode failed");
        }
        if (rs) reed_solomon_release(rs);
        free(p);
    }
    struct mmsghdr msgs[64];
    struct iovec iov[64];
    int queued = 0;
    for (int x = 0; x < total; x++) {
        uint8_t *h = shards + (size_t)x * bs;
        put32le(h + 28, (uint32_t)(x << 12 | data_shards << 22 | fec << 4));
        h[0] = 0x90;  // RTP version 2, with the extension flag
        h[1] = 0;
        put16be(h + 2, (uint16_t)(low_seq + (uint32_t)x));
        put32be(h + 4, timestamp);
        h[27] = multi;
        put32le(h + 20, frame_index);
        iov[queued].iov_base = h;
        iov[queued].iov_len = (size_t)bs;
        memset(&msgs[queued], 0, sizeof(msgs[queued]));
        msgs[queued].msg_hdr.msg_name = (void *)to;
        msgs[queued].msg_hdr.msg_namelen = sizeof(*to);
        msgs[queued].msg_hdr.msg_iov = &iov[queued];
        msgs[queued].msg_hdr.msg_iovlen = 1;
        queued++;
        if (queued == 64 || x == total - 1) {
            int off = 0;
            while (off < queued) {
                int r = sendmmsg(sock, msgs + off, (unsigned)(queued - off), 0);
                if (r < 0) {
                    if (errno == EINTR) continue;
                    if (errno == ENOBUFS || errno == EAGAIN) {
                        usleep(200);
                        continue;
                    }
                    break;
                }
                off += r;
                packets_sent += r;
                bytes_sent += (long long)r * bs;
            }
            queued = 0;
        }
    }
    low_seq += (uint32_t)total;
    free(shards);
}

// One encoded frame out: an 8-byte short header, then the frame, cut into packets behind 32-byte headers, in up to
// four FEC blocks. latency: capture to sending, in 0.1 ms.
static void send_frame(const uint8_t *frame, int len, int idr, int latency) {
    struct sockaddr_in to;
    pthread_mutex_lock(&client_lock);
    int ok = have_client;
    to = client;
    pthread_mutex_unlock(&client_lock);
    if (!ok) return;
    frame_index++;
    int bs = PACKET_SIZE + RTP_HEADER;
    int per = bs - HEADER_SIZE;
    int data_len = 8 + len;
    int packets = (data_len + per - 1) / per;
    int last_len = data_len % per;
    if (last_len == 0) last_len = per;
    size_t plen = (size_t)packets * HEADER_SIZE + (size_t)data_len;
    uint8_t *payload = calloc(1, plen);
    if (!payload) return;
    uint8_t head[8] = {0};
    head[0] = 0x01;
    put16le(head + 1, (uint32_t)(latency < 0 ? 0 : latency > 65535 ? 65535 : latency));
    head[3] = idr ? 2 : 1;
    put16le(head + 4, (uint32_t)last_len);
    int src = 0;
    for (int p = 0; p < packets; p++) {
        size_t at = (size_t)p * (HEADER_SIZE + per) + HEADER_SIZE;
        int take = per < data_len - src ? per : data_len - src;
        for (int i = 0; i < take; i++, src++) payload[at + i] = src < 8 ? head[src] : frame[src - 8];
    }
    int fec = FEC_PERCENT;
    int max_data_shards = 255 * 100 / (100 + fec);
    size_t max_per_block = (size_t)max_data_shards * bs;
    int blocks = (int)((plen + max_per_block - 1) / max_per_block);
    if (blocks > MAX_FEC_BLOCKS) {
        fec = 0;
        blocks = MAX_FEC_BLOCKS;
    }
    size_t unaligned = plen / blocks;
    size_t aligned = (unaligned + bs - 1) / bs * bs;
    uint32_t timestamp = (uint32_t)((now_s() - epoch) * 90000.0);
    for (int b = 0; b < blocks; b++) {
        size_t start = (size_t)b * aligned;
        if (start >= plen) break;
        size_t l = b == blocks - 1 ? plen - start : (aligned < plen - start ? aligned : plen - start);
        send_block(&to, payload + start, (int)l, b, blocks, fec, timestamp);
    }
    frames_sent++;
    free(payload);
}

// ---- the picture from KWin through PipeWire ----

static pthread_mutex_t frame_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t frame_cond = PTHREAD_COND_INITIALIZER;
static uint8_t *frame_buf;
static int frame_w, frame_h, frame_stride, frame_format;  // frame_format: an AVPixelFormat
static uint64_t frame_seq;
static double frame_time;
static long long frames_captured;

static struct pw_thread_loop *pw_loop;
static struct pw_stream *pw_stream_;
static struct spa_hook stream_hook;
static struct spa_video_info_raw video_info;

static enum AVPixelFormat av_format_of(uint32_t spa) {
    switch (spa) {
        case SPA_VIDEO_FORMAT_BGRx: return AV_PIX_FMT_BGR0;
        case SPA_VIDEO_FORMAT_BGRA: return AV_PIX_FMT_BGRA;
        case SPA_VIDEO_FORMAT_RGBx: return AV_PIX_FMT_RGB0;
        case SPA_VIDEO_FORMAT_RGBA: return AV_PIX_FMT_RGBA;
        case SPA_VIDEO_FORMAT_xRGB: return AV_PIX_FMT_0RGB;
        case SPA_VIDEO_FORMAT_xBGR: return AV_PIX_FMT_0BGR;
        default: return AV_PIX_FMT_NONE;
    }
}

static void on_param_changed(void *data, uint32_t id, const struct spa_pod *param) {
    (void)data;
    if (param == NULL || id != SPA_PARAM_Format) return;
    uint32_t media_type, media_subtype;
    if (spa_format_parse(param, &media_type, &media_subtype) < 0) return;
    if (media_type != SPA_MEDIA_TYPE_video || media_subtype != SPA_MEDIA_SUBTYPE_raw) return;
    memset(&video_info, 0, sizeof(video_info));
    spa_format_video_raw_parse(param, &video_info);
    say("capture %ux%u %s, up to %u/%u fps", video_info.size.width, video_info.size.height,
        spa_debug_type_find_short_name(spa_type_video_format, video_info.format), video_info.max_framerate.num,
        video_info.max_framerate.denom ? video_info.max_framerate.denom : 1);
    uint8_t buffer[1024];
    struct spa_pod_builder b = SPA_POD_BUILDER_INIT(buffer, sizeof(buffer));
    const struct spa_pod *params[1];
    params[0] = spa_pod_builder_add_object(&b, SPA_TYPE_OBJECT_ParamBuffers, SPA_PARAM_Buffers,
        SPA_PARAM_BUFFERS_buffers, SPA_POD_CHOICE_RANGE_Int(4, 2, 16),
        SPA_PARAM_BUFFERS_dataType, SPA_POD_CHOICE_FLAGS_Int((1 << SPA_DATA_MemFd) | (1 << SPA_DATA_MemPtr)));
    pw_stream_update_params(pw_stream_, params, 1);
}

static void on_state_changed(void *data, enum pw_stream_state old, enum pw_stream_state state, const char *error) {
    (void)data;
    (void)old;
    say("pipewire %s%s%s", pw_stream_state_as_string(state), error ? ": " : "", error ? error : "");
    if (state == PW_STREAM_STATE_ERROR || state == PW_STREAM_STATE_UNCONNECTED) {
        if (state == PW_STREAM_STATE_ERROR) quitting = 1;
    }
}

static void on_process(void *data) {
    (void)data;
    struct pw_buffer *b = NULL, *n;
    // The newest buffer only: older ones go straight back.
    while ((n = pw_stream_dequeue_buffer(pw_stream_)) != NULL) {
        if (b) pw_stream_queue_buffer(pw_stream_, b);
        b = n;
    }
    if (b == NULL) return;
    struct spa_buffer *buf = b->buffer;
    struct spa_data *d = &buf->datas[0];
    if (d->data != NULL && d->chunk != NULL && d->chunk->size > 0 && !(d->chunk->flags & SPA_CHUNK_FLAG_CORRUPTED)) {
        int w = (int)video_info.size.width, h = (int)video_info.size.height;
        int stride = d->chunk->stride > 0 ? d->chunk->stride : w * 4;
        enum AVPixelFormat f = av_format_of(video_info.format);
        const uint8_t *src = (const uint8_t *)d->data + d->chunk->offset;
        if (w > 0 && h > 0 && f != AV_PIX_FMT_NONE && (size_t)stride * h <= d->maxsize) {
            pthread_mutex_lock(&frame_lock);
            if (frame_buf == NULL || frame_w != w || frame_h != h) {
                free(frame_buf);
                frame_buf = malloc((size_t)w * 4 * h);
            }
            if (frame_buf) {
                for (int y = 0; y < h; y++) memcpy(frame_buf + (size_t)y * w * 4, src + (size_t)y * stride, (size_t)w * 4);
                frame_w = w;
                frame_h = h;
                frame_stride = w * 4;
                frame_format = f;
                frame_seq++;
                frame_time = now_s();
                frames_captured++;
                pthread_cond_signal(&frame_cond);
            }
            pthread_mutex_unlock(&frame_lock);
        }
    }
    pw_stream_queue_buffer(pw_stream_, b);
}

static const struct pw_stream_events stream_events = {
    PW_VERSION_STREAM_EVENTS,
    .state_changed = on_state_changed,
    .param_changed = on_param_changed,
    .process = on_process,
};

static int capture_start(uint32_t node, uint64_t serial) {
    pw_init(NULL, NULL);
    pw_loop = pw_thread_loop_new("pxr-capture", NULL);
    struct pw_context *ctx = pw_context_new(pw_thread_loop_get_loop(pw_loop), NULL, 0);
    if (!ctx) return -1;
    if (pw_thread_loop_start(pw_loop) < 0) return -1;
    pw_thread_loop_lock(pw_loop);
    struct pw_core *core = pw_context_connect(ctx, NULL, 0);
    if (!core) {
        pw_thread_loop_unlock(pw_loop);
        return -1;
    }
    struct pw_properties *props = pw_properties_new(PW_KEY_MEDIA_TYPE, "Video", PW_KEY_MEDIA_CATEGORY, "Capture",
                                                    PW_KEY_MEDIA_ROLE, "Screen", NULL);
    if (serial) pw_properties_setf(props, PW_KEY_TARGET_OBJECT, "%" PRIu64, serial);
    pw_stream_ = pw_stream_new(core, "Pocket XR host", props);
    pw_stream_add_listener(pw_stream_, &stream_hook, &stream_events, NULL);
    uint8_t buffer[1024];
    struct spa_pod_builder b = SPA_POD_BUILDER_INIT(buffer, sizeof(buffer));
    struct spa_rectangle def = SPA_RECTANGLE((uint32_t)W, (uint32_t)H), min = SPA_RECTANGLE(1, 1),
                         max = SPA_RECTANGLE(8192, 8192);
    struct spa_fraction zero = SPA_FRACTION(0, 1), want = SPA_FRACTION((uint32_t)FPS, 1), most = SPA_FRACTION(1000, 1);
    const struct spa_pod *params[1];
    params[0] = spa_pod_builder_add_object(&b, SPA_TYPE_OBJECT_Format, SPA_PARAM_EnumFormat,
        SPA_FORMAT_mediaType, SPA_POD_Id(SPA_MEDIA_TYPE_video),
        SPA_FORMAT_mediaSubtype, SPA_POD_Id(SPA_MEDIA_SUBTYPE_raw),
        SPA_FORMAT_VIDEO_format, SPA_POD_CHOICE_ENUM_Id(5, SPA_VIDEO_FORMAT_BGRx, SPA_VIDEO_FORMAT_BGRx,
                                                        SPA_VIDEO_FORMAT_BGRA, SPA_VIDEO_FORMAT_RGBx, SPA_VIDEO_FORMAT_RGBA),
        SPA_FORMAT_VIDEO_size, SPA_POD_CHOICE_RANGE_Rectangle(&def, &min, &max),
        SPA_FORMAT_VIDEO_framerate, SPA_POD_Fraction(&zero),
        SPA_FORMAT_VIDEO_maxFramerate, SPA_POD_CHOICE_RANGE_Fraction(&want, &zero, &most));
    int r = pw_stream_connect(pw_stream_, PW_DIRECTION_INPUT, serial ? PW_ID_ANY : node,
                              PW_STREAM_FLAG_AUTOCONNECT | PW_STREAM_FLAG_MAP_BUFFERS, params, 1);
    pw_thread_loop_unlock(pw_loop);
    return r;
}

static void capture_stop(void) {
    if (!pw_loop) return;
    pw_thread_loop_lock(pw_loop);
    if (pw_stream_) {
        pw_stream_disconnect(pw_stream_);
        pw_stream_destroy(pw_stream_);
        pw_stream_ = NULL;
    }
    pw_thread_loop_unlock(pw_loop);
    pw_thread_loop_stop(pw_loop);
}

// ---- encoding ----

static AVBufferRef *hwdev;
static AVFilterGraph *graph;
static AVFilterContext *fsrc, *fsink;
static AVCodecContext *enc;
static double encode_ms_sum, encode_ms_max;
static long long encoded, repeats, keys;

static const char *encoder_name(const char *codec) {
    if (strcmp(codec, "hevc") == 0) return "hevc_vaapi";
    if (strcmp(codec, "av1") == 0) return "av1_vaapi";
    return "h264_vaapi";
}

// The picture to the graphics card and converted there to NV12 (BT.709, limited range, as the encoder describes it).
// Each filter gets the VAAPI device before it starts: hwupload needs it then.
static int filters_open(int w, int h, enum AVPixelFormat fmt) {
    char args[256];
    graph = avfilter_graph_alloc();
    if (!graph) return -1;
    snprintf(args, sizeof(args), "video_size=%dx%d:pix_fmt=%d:time_base=1/%d:pixel_aspect=1/1", w, h, (int)fmt, FPS);
    if (avfilter_graph_create_filter(&fsrc, avfilter_get_by_name("buffer"), "in", args, NULL, graph) < 0) return -2;
    if (avfilter_graph_create_filter(&fsink, avfilter_get_by_name("buffersink"), "out", NULL, NULL, graph) < 0)
        return -3;
    AVFilterContext *up = avfilter_graph_alloc_filter(graph, avfilter_get_by_name("hwupload"), "up");
    AVFilterContext *conv = avfilter_graph_alloc_filter(graph, avfilter_get_by_name("scale_vaapi"), "conv");
    if (!up || !conv) return -4;
    up->hw_device_ctx = av_buffer_ref(hwdev);
    conv->hw_device_ctx = av_buffer_ref(hwdev);
    snprintf(args, sizeof(args), "w=%d:h=%d:format=nv12:out_color_matrix=bt709:out_range=tv", W, H);
    int r;
    if ((r = avfilter_init_str(up, NULL)) < 0 || (r = avfilter_init_str(conv, args)) < 0) {
        char e[128];
        av_strerror(r, e, sizeof(e));
        say("error filters: %s", e);
        return -5;
    }
    if (avfilter_link(fsrc, 0, up, 0) < 0 || avfilter_link(up, 0, conv, 0) < 0 || avfilter_link(conv, 0, fsink, 0) < 0)
        return -6;
    if ((r = avfilter_graph_config(graph, NULL)) < 0) {
        char e[128];
        av_strerror(r, e, sizeof(e));
        say("error filters: %s", e);
        return -7;
    }
    return 0;
}

static AVCodecContext *encoder_open(const char *codec, AVBufferRef *frames, int w, int h, char *why, size_t why_len) {
    const AVCodec *c = avcodec_find_encoder_by_name(encoder_name(codec));
    if (!c) {
        snprintf(why, why_len, "no %s in this FFmpeg", encoder_name(codec));
        return NULL;
    }
    AVCodecContext *e = avcodec_alloc_context3(c);
    e->width = w;
    e->height = h;
    e->time_base = (AVRational){1, FPS};
    e->framerate = (AVRational){FPS, 1};
    e->pix_fmt = AV_PIX_FMT_VAAPI;
    e->hw_frames_ctx = av_buffer_ref(frames);
    e->gop_size = INT16_MAX;  // a key frame only when asked
    e->keyint_min = INT16_MAX;
    e->max_b_frames = 0;
    // Peak-limited variable bitrate: a quiet screen sends small frames (constant bitrate lagged on Windows, 20 Nov 2026).
    e->bit_rate = (int64_t)KBPS * 750;
    e->rc_max_rate = (int64_t)KBPS * 1000;
    e->rc_buffer_size = (int)((int64_t)KBPS * 1000 * 3 / 2 / FPS);
    e->color_range = AVCOL_RANGE_MPEG;
    e->colorspace = AVCOL_SPC_BT709;
    e->color_primaries = AVCOL_PRI_BT709;
    e->color_trc = AVCOL_TRC_BT709;
    e->flags |= AV_CODEC_FLAG_LOW_DELAY;
    if (strcmp(codec, "h264") == 0) {
        e->profile = AV_PROFILE_H264_HIGH;
        av_opt_set_int(e->priv_data, "sei", 0, 0);
    } else if (strcmp(codec, "hevc") == 0) {
        e->profile = AV_PROFILE_HEVC_MAIN;
    }
    av_opt_set(e->priv_data, "rc_mode", "VBR", 0);
    av_opt_set_int(e->priv_data, "async_depth", 1, 0);
    av_opt_set_int(e->priv_data, "aud", 0, 0);
    int r = avcodec_open2(e, c, NULL);
    if (r < 0) {
        av_strerror(r, why, why_len);
        avcodec_free_context(&e);
        return NULL;
    }
    return e;
}

static int probe(void) {
    if (av_hwdevice_ctx_create(&hwdev, AV_HWDEVICE_TYPE_VAAPI, DEVICE, NULL, 0) < 0) {
        say("codecs");
        say("error no VAAPI device at %s", DEVICE);
        return 1;
    }
    char line[128] = "codecs";
    const char *all[] = {"h264", "hevc", "av1"};
    for (int i = 0; i < 3; i++) {
        AVBufferRef *fr = av_hwframe_ctx_alloc(hwdev);
        AVHWFramesContext *fc = (AVHWFramesContext *)fr->data;
        fc->format = AV_PIX_FMT_VAAPI;
        fc->sw_format = AV_PIX_FMT_NV12;
        fc->width = 640;
        fc->height = 368;
        fc->initial_pool_size = 4;
        char why[128] = "";
        if (av_hwframe_ctx_init(fr) == 0) {
            W = 640;
            H = 368;
            AVCodecContext *e = encoder_open(all[i], fr, 640, 368, why, sizeof(why));
            if (e) {
                strcat(line, " ");
                strcat(line, all[i]);
                avcodec_free_context(&e);
            }
        }
        av_buffer_unref(&fr);
    }
    say("%s", line);
    return 0;
}

static void encode_and_send(AVFrame *hw, double taken) {
    int want_idr = atomic_exchange(&idr_wanted, 0);
    if (want_idr) {
        hw->pict_type = AV_PICTURE_TYPE_I;
        hw->flags |= AV_FRAME_FLAG_KEY;
    } else {
        hw->pict_type = AV_PICTURE_TYPE_NONE;
        hw->flags &= ~AV_FRAME_FLAG_KEY;
    }
    static int64_t pts;
    hw->pts = pts++;
    double t0 = now_s();
    if (avcodec_send_frame(enc, hw) < 0) {
        if (want_idr) idr_wanted = 1;
        return;
    }
    AVPacket *pkt = av_packet_alloc();
    while (avcodec_receive_packet(enc, pkt) == 0) {
        double ms = (now_s() - t0) * 1000.0;
        encode_ms_sum += ms;
        if (ms > encode_ms_max) encode_ms_max = ms;
        encoded++;
        int key = (pkt->flags & AV_PKT_FLAG_KEY) != 0;
        if (key) keys++;
        send_frame(pkt->data, pkt->size, key, (int)((now_s() - taken) * 10000.0));
        av_packet_unref(pkt);
    }
    av_packet_free(&pkt);
}

static void *encoder_thread(void *arg) {
    (void)arg;
    AVFrame *sw = av_frame_alloc(), *hw = av_frame_alloc();
    uint8_t *mine = NULL;
    uint64_t seen = 0;
    double last_sent = 0, taken = 0;
    int w = 0, h = 0, fmt = AV_PIX_FMT_NONE;
    double min_gap = 1.0 / (FPS > 0 ? FPS : 60);
    while (!quitting) {
        pthread_mutex_lock(&frame_lock);
        struct timespec until;
        clock_gettime(CLOCK_REALTIME, &until);
        until.tv_nsec += 100 * 1000000L;  // a still screen: its last picture again ten times a second
        if (until.tv_nsec >= 1000000000L) {
            until.tv_sec++;
            until.tv_nsec -= 1000000000L;
        }
        while (frame_seq == seen && !quitting) {
            if (pthread_cond_timedwait(&frame_cond, &frame_lock, &until) == ETIMEDOUT) break;
        }
        int fresh = frame_seq != seen;
        if (fresh) {
            if (mine == NULL || w != frame_w || h != frame_h) {
                free(mine);
                mine = malloc((size_t)frame_stride * frame_h);
            }
            if (mine) memcpy(mine, frame_buf, (size_t)frame_stride * frame_h);
            w = frame_w;
            h = frame_h;
            fmt = frame_format;
            seen = frame_seq;
            taken = frame_time;
        }
        pthread_mutex_unlock(&frame_lock);
        if (quitting) break;
        if (mine == NULL || w == 0) continue;
        if (!fresh) {
            repeats++;
            taken = now_s();
        }
        // No faster than the stream's rate.
        double gap = now_s() - last_sent;
        if (gap < min_gap) usleep((useconds_t)((min_gap - gap) * 1e6));
        if (graph == NULL) {
            int r = filters_open(w, h, (enum AVPixelFormat)fmt);
            if (r < 0) {
                say("error the picture's converter did not open (%d)", r);
                quitting = 1;
                break;
            }
            char why[160] = "";
            enc = encoder_open(CODEC, av_buffersink_get_hw_frames_ctx(fsink), W, H, why, sizeof(why));
            if (!enc) {
                say("error %s did not open: %s", encoder_name(CODEC), why);
                quitting = 1;
                break;
            }
            say("encoder %s %dx%d@%d, up to %d kbps", encoder_name(CODEC), W, H, FPS, KBPS);
        }
        sw->format = fmt;
        sw->width = w;
        sw->height = h;
        sw->data[0] = mine;
        sw->linesize[0] = w * 4;
        sw->pts = 0;
        if (av_buffersrc_add_frame_flags(fsrc, sw, AV_BUFFERSRC_FLAG_KEEP_REF) < 0) continue;
        while (av_buffersink_get_frame(fsink, hw) == 0) {
            encode_and_send(hw, taken);
            av_frame_unref(hw);
        }
        last_sent = now_s();
    }
    free(mine);
    av_frame_free(&sw);
    av_frame_free(&hw);
    return NULL;
}

// ---- KWin ----

static struct zkde_screencast_unstable_v1 *screencast;
static uint32_t screencast_version;
static struct zkde_screencast_stream_unstable_v1 *kstream;
static uint32_t node_id;
static uint64_t node_serial;
static int created, failed, closed;

static void registry_global(void *data, struct wl_registry *reg, uint32_t name, const char *iface, uint32_t version) {
    (void)data;
    if (strcmp(iface, zkde_screencast_unstable_v1_interface.name) == 0) {
        screencast_version = version < 6 ? version : 6;
        screencast = wl_registry_bind(reg, name, &zkde_screencast_unstable_v1_interface, screencast_version);
    }
}
static void registry_remove(void *data, struct wl_registry *reg, uint32_t name) {
    (void)data;
    (void)reg;
    (void)name;
}
static const struct wl_registry_listener registry_listener = {registry_global, registry_remove};

static void stream_closed(void *data, struct zkde_screencast_stream_unstable_v1 *s) {
    (void)data;
    (void)s;
    closed = 1;
}
static void stream_created(void *data, struct zkde_screencast_stream_unstable_v1 *s, uint32_t node) {
    (void)data;
    (void)s;
    node_id = node;
    created = 1;
}
static void stream_failed(void *data, struct zkde_screencast_stream_unstable_v1 *s, const char *error) {
    (void)data;
    (void)s;
    say("error KWin: %s", error);
    failed = 1;
}
static void stream_serial(void *data, struct zkde_screencast_stream_unstable_v1 *s, uint32_t hi, uint32_t lo) {
    (void)data;
    (void)s;
    node_serial = ((uint64_t)hi << 32) | lo;
}
static const struct zkde_screencast_stream_unstable_v1_listener stream_listener = {
    stream_closed, stream_created, stream_failed, stream_serial};

// ---- main ----

static void stop_signal(int sig) {
    (void)sig;
    quitting = 1;
}

static void *stdin_thread(void *arg) {
    (void)arg;
    char line[256];
    while (!quitting && fgets(line, sizeof(line), stdin)) {
        if (strncmp(line, "idr", 3) == 0) idr_wanted = 1;
        else if (strncmp(line, "quit", 4) == 0) quitting = 1;
    }
    quitting = 1;  // the host went away
    return NULL;
}

static void *stats_thread(void *arg) {
    (void)arg;
    long long f0 = 0, b0 = 0, c0 = 0, e0 = 0, r0 = 0, k0 = 0;
    double ms0 = 0;
    while (!quitting) {
        for (int i = 0; i < 100 && !quitting; i++) usleep(100000);
        if (quitting) break;
        long long e = encoded - e0;
        say("stats %.1f frames/s sent, %.1f Mbps, %.1f captured/s, %lld repeats, %lld key, encode %.1f ms (max %.1f)",
            (frames_sent - f0) / 10.0, (bytes_sent - b0) * 8 / 10.0 / 1e6, (frames_captured - c0) / 10.0,
            repeats - r0, keys - k0, e > 0 ? (encode_ms_sum - ms0) / e : 0.0, encode_ms_max);
        f0 = frames_sent;
        b0 = bytes_sent;
        c0 = frames_captured;
        e0 = encoded;
        r0 = repeats;
        k0 = keys;
        ms0 = encode_ms_sum;
        encode_ms_max = 0;
    }
    return NULL;
}

static const char *arg(int argc, char **argv, const char *name) {
    for (int i = 1; i + 1 < argc; i++)
        if (strcmp(argv[i], name) == 0) return argv[i + 1];
    return NULL;
}

int main(int argc, char **argv) {
    setvbuf(stdout, NULL, _IOLBF, 0);
    signal(SIGPIPE, SIG_IGN);
    signal(SIGTERM, stop_signal);
    signal(SIGINT, stop_signal);
    av_log_set_level(AV_LOG_ERROR);
    const char *v;
    if ((v = arg(argc, argv, "--device"))) DEVICE = v;
    for (int i = 1; i < argc; i++)
        if (strcmp(argv[i], "--probe") == 0) return probe();
    if ((v = arg(argc, argv, "--width"))) W = atoi(v);
    if ((v = arg(argc, argv, "--height"))) H = atoi(v);
    if ((v = arg(argc, argv, "--fps"))) FPS = atoi(v);
    if ((v = arg(argc, argv, "--kbps"))) KBPS = atoi(v);
    if ((v = arg(argc, argv, "--codec"))) CODEC = v;
    if ((v = arg(argc, argv, "--port"))) PORT = atoi(v);
    if ((v = arg(argc, argv, "--packet-size"))) PACKET_SIZE = atoi(v);
    if ((v = arg(argc, argv, "--min-fec"))) MIN_FEC = atoi(v);
    if ((v = arg(argc, argv, "--fec"))) FEC_PERCENT = atoi(v);
    if ((v = arg(argc, argv, "--name"))) NAME = v;
    if ((v = arg(argc, argv, "--description"))) DESCRIPTION = v;
    if ((v = arg(argc, argv, "--seconds"))) SECONDS = atof(v);
    if (W < 64 || H < 64 || W > 8192 || H > 8192 || FPS < 1 || FPS > 240 || PACKET_SIZE < 200 || PACKET_SIZE > 9000) {
        say("error bad settings");
        return 2;
    }
    W &= ~1;  // the encoders want even sizes
    H &= ~1;
    epoch = now_s();
    reed_solomon_init();

    // The video port first, so the phone's pings are heard from the start.
    if (PORT > 0) {
        sock = socket(AF_INET, SOCK_DGRAM, 0);
        int one = 1, size = 4 << 20;
        setsockopt(sock, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
        setsockopt(sock, SOL_SOCKET, SO_SNDBUF, &size, sizeof(size));
        int tos = 0xa0;  // CS5: video, for Wi-Fi's video queue
        setsockopt(sock, IPPROTO_IP, IP_TOS, &tos, sizeof(tos));
        struct timeval tv = {0, 200000};
        setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
        struct sockaddr_in a = {0};
        a.sin_family = AF_INET;
        a.sin_port = htons((uint16_t)PORT);
        a.sin_addr.s_addr = htonl(INADDR_ANY);
        if (bind(sock, (struct sockaddr *)&a, sizeof(a)) < 0) {
            say("error the video port %d: %s", PORT, strerror(errno));
            return 3;
        }
        pthread_t t;
        pthread_create(&t, NULL, pings, NULL);
    }

    if (av_hwdevice_ctx_create(&hwdev, AV_HWDEVICE_TYPE_VAAPI, DEVICE, NULL, 0) < 0) {
        say("error no VAAPI device at %s", DEVICE);
        return 4;
    }

    struct wl_display *dpy = wl_display_connect(NULL);
    if (!dpy) {
        say("error no Wayland display (is this KDE Plasma's desktop?)");
        return 5;
    }
    struct wl_registry *reg = wl_display_get_registry(dpy);
    wl_registry_add_listener(reg, &registry_listener, NULL);
    wl_display_roundtrip(dpy);
    if (!screencast || screencast_version < 2) {
        say("error KWin did not offer its screencast to this program (version %u): its desktop file with "
            "X-KDE-Wayland-Interfaces=zkde_screencast_unstable_v1 is missing",
            screencast_version);
        return 6;
    }
    // The pointer drawn into the picture (2: embedded).
    if (screencast_version >= 4)
        kstream = zkde_screencast_unstable_v1_stream_virtual_output_with_description(
            screencast, NAME, DESCRIPTION, W, H, wl_fixed_from_int(1), 2);
    else
        kstream = zkde_screencast_unstable_v1_stream_virtual_output(screencast, NAME, W, H, wl_fixed_from_int(1), 2);
    zkde_screencast_stream_unstable_v1_add_listener(kstream, &stream_listener, NULL);
    double t0 = now_s();
    while (!created && !failed && !closed && now_s() - t0 < 10) {
        if (wl_display_roundtrip(dpy) < 0) break;
        if (!created) usleep(20000);
    }
    if (!created) {
        say("error KWin made no virtual output");
        return 7;
    }
    say("output Virtual-%s node %u serial %" PRIu64, NAME, node_id, node_serial);
    if (capture_start(node_id, node_serial) < 0) {
        say("error PipeWire did not connect");
        return 8;
    }

    pthread_t te, ts, ti;
    pthread_create(&te, NULL, encoder_thread, NULL);
    pthread_create(&ts, NULL, stats_thread, NULL);
    pthread_create(&ti, NULL, stdin_thread, NULL);
    pthread_detach(ti);

    int fd = wl_display_get_fd(dpy);
    while (!quitting && !closed) {
        if (SECONDS > 0 && now_s() - t0 > SECONDS) break;
        while (wl_display_prepare_read(dpy) != 0) wl_display_dispatch_pending(dpy);
        wl_display_flush(dpy);
        struct pollfd p = {fd, POLLIN, 0};
        if (poll(&p, 1, 200) > 0) wl_display_read_events(dpy);
        else wl_display_cancel_read(dpy);
        wl_display_dispatch_pending(dpy);
    }
    if (closed) say("error KWin closed the virtual output");
    quitting = 1;
    pthread_cond_broadcast(&frame_cond);
    pthread_join(te, NULL);
    pthread_join(ts, NULL);
    capture_stop();
    zkde_screencast_stream_unstable_v1_close(kstream);
    wl_display_roundtrip(dpy);
    wl_display_disconnect(dpy);
    if (enc) avcodec_free_context(&enc);
    if (graph) avfilter_graph_free(&graph);
    say("ended");
    fflush(stdout);
    _exit(0);  // not exit(): a library's exit handler held the process once (20 Nov 2026)
}
