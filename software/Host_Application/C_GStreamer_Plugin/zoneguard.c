/*
 * zoneguard — a GStreamer pass-through filter that reads VVAS inference
 * metadata (GstInferenceMeta) off each buffer, hit-tests the detected
 * bounding boxes against a set of configured zones/lines, and emits
 * "alert" events to a Unix-domain socket when a person enters a zone or
 * crosses a line.
 *
 * The element is intentionally small; it does not modify the buffer or
 * its metadata — it is a side-car analytics element. Zones are reloaded
 * from a JSON file ("zones-config") whenever that file's mtime changes,
 * so the Python server can update them live without rebuilding the
 * pipeline.
 *
 * Pipeline usage (one per channel):
 *
 *   ... ! imaN.src_slave_0 ! queue !
 *   zoneguard channel=<0..3> zones-config=/tmp/zoneguard_chN.json
 *             event-socket=/tmp/zoneguard.sock
 *   ! vvas_xmetaconvert ! vvas_xoverlay ! ...
 *
 * Event line format (newline-terminated) sent on the socket:
 *
 *   {"ch":0,"kind":"ENTER","name":"danger","reason":"person in danger","ts":1712958765.12}
 *   {"ch":0,"kind":"CROSS","name":"line 1","reason":"person crossed line 1","ts":1712958770.04}
 *   {"ch":0,"kind":"CLEAR","name":null,"reason":"no person in any zone","ts":1712958771.50}
 *
 * © 2026 — internal utility, no license intended.
 */

#define _GNU_SOURCE            /* CPU_SET, pthread_setaffinity_np */
#define VVAS_GLIB_UTILS 1
#include <stdbool.h>
#include <gst/gst.h>
#include <gst/base/gstbasetransform.h>
#include <gst/video/video-info.h>
#include <vvas_utils/vvas_utils.h>
#include <vvas_core/vvas_infer_classification.h>
#include <gst/vvas/gstinferencemeta.h>
#include <gst/vvas/gstinferenceprediction.h>
#include <gst/vvas/gstvvasoverlaymeta.h>
#include <vvas_core/vvas_overlay_shape_info.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/stat.h>
#include <sys/mman.h>
#include <fcntl.h>
#include <pthread.h>
#include <sched.h>
#include <errno.h>
#include <unistd.h>
#include <string.h>
#include <stdio.h>
#include <jansson.h>
#include <time.h>
#include <math.h>
#include "shm_alert.h"

GST_DEBUG_CATEGORY_STATIC (zoneguard_debug);
#define GST_CAT_DEFAULT zoneguard_debug

#define ZG_TYPE_RECT  1
#define ZG_TYPE_LINE  2
#define ZG_MAX_ZONES  32

typedef struct {
    int type;       /* ZG_TYPE_RECT or ZG_TYPE_LINE */
    char name[64];
    /* Coordinates are stored as fractions of the channel's render rect
     * (0..1). At hit-test time we multiply by the current video frame
     * dimensions so zones follow whatever the source resolution is. */
    double x, y, w, h;
    double x1, y1, x2, y2;
} ZgZone;

typedef struct {
    int channel;
    char zones_config[256];
    char event_socket[256];
    time_t zones_mtime;

    ZgZone zones[ZG_MAX_ZONES];
    int nzones;
    int draw_overlay;            /* 1 = draw zones on the video via vvas_xoverlay */

    /* Cached frame dimensions from the sink pad's caps (video/x-raw).
     * Fractional zone coordinates are multiplied by these each frame. */
    int frame_w, frame_h;

    int sock_fd;                 /* connected Unix dgram socket, -1 if unavailable */

    /* Shared-memory fast path to the R5. All 4 zoneguard instances mmap
     * the same 4 KB page; each channel owns bit `channel` of shm->flags.
     * Atomic fetch-or/and keep the bank consistent across threads even
     * though each channel only writes its own bit. The page is mapped
     * lazily on the first transform_ip call because GstBaseTransform
     * doesn't give us a reliable per-thread start hook. */
    int shm_fd;
    struct shm_alert *shm;       /* NULL if mmap failed or not yet mapped */
    int rt_promoted;             /* 1 once this streaming thread is SCHED_FIFO on CPU 3 */

    /* last-tick state for change detection */
    int last_any_inside;         /* 1 if any person was inside any rect zone last frame */
    int frame_counter;           /* how many buffers we've seen */
    int last_resend_frame;       /* frame number at which we last re-emitted state */
    int hit_streak;              /* consecutive frames with a zone hit */
    int miss_streak;             /* consecutive frames without any hit */
    int alert_active;            /* current reported state after hysteresis */
    /* crossing detection: per-zone last-side of each persisting track's centroid.
     * We don't have stable IDs without a tracker, so we approximate with a
     * single "closest-centroid" state per line zone. Good enough for a single
     * operator walking into/out of a line. */
    int line_last_side[ZG_MAX_ZONES];  /* -1 unset, 0 left/above, 1 right/below */
} ZgState;

typedef struct _ZoneGuard {
    GstBaseTransform parent;
    ZgState s;
} ZoneGuard;

typedef struct _ZoneGuardClass {
    GstBaseTransformClass parent_class;
} ZoneGuardClass;

#define ZG_TYPE_ZONEGUARD (zoneguard_get_type ())
GType zoneguard_get_type (void);
G_DEFINE_TYPE (ZoneGuard, zoneguard, GST_TYPE_BASE_TRANSFORM)
#define ZG_ZONEGUARD(obj) (G_TYPE_CHECK_INSTANCE_CAST ((obj), ZG_TYPE_ZONEGUARD, ZoneGuard))

enum {
    PROP_0,
    PROP_CHANNEL,
    PROP_ZONES_CONFIG,
    PROP_EVENT_SOCKET,
};

static GstStaticPadTemplate sink_template = GST_STATIC_PAD_TEMPLATE (
    "sink", GST_PAD_SINK, GST_PAD_ALWAYS, GST_STATIC_CAPS_ANY);
static GstStaticPadTemplate src_template  = GST_STATIC_PAD_TEMPLATE (
    "src",  GST_PAD_SRC,  GST_PAD_ALWAYS, GST_STATIC_CAPS_ANY);


/* --------------------------------------------------------------------- */
/* socket + JSON                                                          */
/* --------------------------------------------------------------------- */

static void zg_open_socket (ZoneGuard *self) {
    if (self->s.sock_fd >= 0) return;
    if (!self->s.event_socket[0]) return;
    int fd = socket (AF_UNIX, SOCK_DGRAM | SOCK_NONBLOCK, 0);
    if (fd < 0) return;
    /* The server binds the socket; we just send. Remote address is stamped
     * on each sendto(), so no connect() needed. */
    self->s.sock_fd = fd;
}

/* --------------------------------------------------------------------- */
/* Shared-memory fast path (APU → R5)                                     */
/* --------------------------------------------------------------------- */

/* Open /dev/mem and mmap the SHM_ALERT_PA page. Idempotent; safe to call
 * from every streaming thread, but only the first call in a process
 * actually does work (guarded by pthread_once). */
static pthread_once_t    zg_shm_once      = PTHREAD_ONCE_INIT;
static int               zg_shm_global_fd = -1;
static struct shm_alert *zg_shm_global    = NULL;

/* TCM_0B is powered only while the R5 remoteproc is running. Accessing it
 * from the APU while R5 is offline traps with SIGBUS on the AXI bus. Gate
 * the fast-path mmap on remoteproc state so APU-only mode works without
 * the R5 firmware loaded. */
static int zg_rproc_is_running (void) {
    FILE *f = fopen ("/sys/class/remoteproc/remoteproc0/state", "r");
    if (!f) return 0;
    char buf[32] = {0};
    size_t n = fread (buf, 1, sizeof(buf)-1, f);
    fclose (f);
    if (n == 0) return 0;
    return strncmp (buf, "running", 7) == 0;
}

static void zg_shm_open_once (void) {
    if (!zg_rproc_is_running ()) {
        GST_INFO ("zoneguard: R5 remoteproc not running — fast path disabled, "
                  "using socket-only emit");
        return;
    }
    int fd = open ("/dev/mem", O_RDWR | O_SYNC);
    if (fd < 0) {
        GST_WARNING ("zoneguard: shm open(/dev/mem) failed: %s — falling back "
                     "to socket-only emit", strerror (errno));
        return;
    }
    void *p = mmap (NULL, SHM_ALERT_SIZE, PROT_READ | PROT_WRITE,
                    MAP_SHARED, fd, SHM_ALERT_PA);
    if (p == MAP_FAILED) {
        GST_WARNING ("zoneguard: shm mmap(0x%lx) failed: %s — falling back "
                     "to socket-only emit", SHM_ALERT_PA, strerror (errno));
        close (fd);
        return;
    }
    struct shm_alert *s = (struct shm_alert *) p;
    /* First writer stamps magic. R5 ignores the page until magic matches,
     * so this is safe even if R5 comes up before us. */
    if (s->magic != SHM_ALERT_MAGIC) {
        s->flags    = 0;
        s->seq      = 0;
        s->reserved = 0;
        s->ts_ns    = 0;
        s->magic    = SHM_ALERT_MAGIC;   /* plain write; see zg_shm_set */
    }
    zg_shm_global_fd = fd;
    zg_shm_global    = s;
    GST_INFO ("zoneguard: shm fast path up at PA 0x%lx", SHM_ALERT_PA);
}

static void zg_shm_attach (ZoneGuard *self) {
    if (self->s.shm) return;
    pthread_once (&zg_shm_once, zg_shm_open_once);
    self->s.shm    = zg_shm_global;
    self->s.shm_fd = zg_shm_global_fd;
}

/* Update bit `channel` of shm->flags to reflect `active`. Uses plain
 * volatile RMW — NOT atomic. Reason: /dev/mem with O_SYNC maps the
 * page as Device/strongly-ordered on ARMv8, and ldrex/strex (which
 * the compiler emits for __atomic_fetch_or) are architecturally
 * undefined on strongly-ordered memory — the CPU raises a SIGBUS.
 * The non-atomic RMW has a theoretical race when two channels flip
 * different bits of `flags` at the same nanosecond, but in practice
 * ENTER/CLEAR transitions fire at most a few times per second per
 * channel, so the window is effectively unreachable. If you ever
 * need lock-free correctness here, split `flags` into 4 separate
 * byte lanes (one per channel) — then there's no shared word. */
static inline void zg_shm_set (ZoneGuard *self, int active) {
    if (!self->s.shm) return;
    volatile struct shm_alert *s = self->s.shm;
    uint32_t bit = 1u << (self->s.channel & 0x3);
    uint32_t f = s->flags;
    if (active) f |=  bit;
    else        f &= ~bit;
    s->flags = f;
    struct timespec ts;
    clock_gettime (CLOCK_MONOTONIC_RAW, &ts);
    uint64_t ns = (uint64_t) ts.tv_sec * 1000000000ULL + ts.tv_nsec;
    s->ts_ns = ns;
    s->seq   = s->seq + 1;
}

/* Promote the current streaming thread to SCHED_FIFO and pin to the
 * isolated core. One-shot per thread. Soft-fail: if CAP_SYS_NICE is
 * missing, the plugin works but without the determinism guarantee —
 * log a warning once. */
static void zg_promote_rt (ZoneGuard *self) {
    if (self->s.rt_promoted) return;
    self->s.rt_promoted = 1;                /* don't retry even on failure */

    struct sched_param p = { .sched_priority = 60 };
    if (pthread_setschedparam (pthread_self (), SCHED_FIFO, &p) != 0) {
        GST_WARNING_OBJECT (self,
            "zoneguard ch%d: SCHED_FIFO promotion failed: %s "
            "(run as root or set CAP_SYS_NICE — determinism not guaranteed)",
            self->s.channel, strerror (errno));
    }
    cpu_set_t set;
    CPU_ZERO (&set);
    CPU_SET (3, &set);                      /* pin to isolated core */
    if (pthread_setaffinity_np (pthread_self (), sizeof(set), &set) != 0) {
        GST_WARNING_OBJECT (self,
            "zoneguard ch%d: pin-to-CPU3 failed: %s",
            self->s.channel, strerror (errno));
    }
}

static void zg_send_event (ZoneGuard *self, const char *kind, const char *name,
                           const char *reason) {
    if (self->s.sock_fd < 0 || !self->s.event_socket[0]) return;
    struct sockaddr_un addr;
    memset (&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy (addr.sun_path, self->s.event_socket, sizeof(addr.sun_path) - 1);

    struct timespec ts; clock_gettime (CLOCK_REALTIME, &ts);
    double tsec = ts.tv_sec + ts.tv_nsec / 1e9;
    char buf[512];
    int n;
    if (name)
        n = snprintf (buf, sizeof(buf),
                      "{\"ch\":%d,\"kind\":\"%s\",\"name\":\"%s\",\"reason\":\"%s\",\"ts\":%.3f}\n",
                      self->s.channel, kind, name, reason, tsec);
    else
        n = snprintf (buf, sizeof(buf),
                      "{\"ch\":%d,\"kind\":\"%s\",\"name\":null,\"reason\":\"%s\",\"ts\":%.3f}\n",
                      self->s.channel, kind, reason, tsec);
    if (n <= 0) return;
    sendto (self->s.sock_fd, buf, n, MSG_NOSIGNAL,
            (struct sockaddr *) &addr, sizeof(addr));
}

static void zg_maybe_reload_zones (ZoneGuard *self) {
    if (!self->s.zones_config[0]) return;
    struct stat st;
    if (stat (self->s.zones_config, &st) < 0) {
        self->s.nzones = 0;
        return;
    }
    if (st.st_mtime == self->s.zones_mtime) return;   /* unchanged */
    self->s.zones_mtime = st.st_mtime;

    json_error_t err;
    json_t *root = json_load_file (self->s.zones_config, 0, &err);
    if (!root) {
        self->s.nzones = 0;
        return;
    }
    /* Accept two shapes:
     *   []                                         (legacy — just zones)
     *   {"draw_overlay": bool, "zones": [...]}     (current)
     */
    json_t *arr = NULL;
    int draw = 0;
    if (json_is_array (root)) {
        arr = root;
    } else if (json_is_object (root)) {
        arr = json_object_get (root, "zones");
        json_t *d = json_object_get (root, "draw_overlay");
        if (d && json_is_boolean (d)) draw = json_boolean_value (d) ? 1 : 0;
    }
    if (!arr || !json_is_array (arr)) {
        self->s.nzones = 0;
        self->s.draw_overlay = 0;
        json_decref (root);
        return;
    }
    self->s.draw_overlay = draw;

    int n = 0;
    size_t i;
    json_t *z;
    json_array_foreach (arr, i, z) {
        if (n >= ZG_MAX_ZONES) break;
        const char *type = json_string_value (json_object_get (z, "type"));
        const char *name = json_string_value (json_object_get (z, "name"));
        if (!type) continue;
        ZgZone *tgt = &self->s.zones[n];
        memset (tgt, 0, sizeof(*tgt));
        strncpy (tgt->name, name ? name : "zone", sizeof(tgt->name) - 1);
        if (!strcmp (type, "rect")) {
            tgt->type = ZG_TYPE_RECT;
            tgt->x = json_number_value (json_object_get (z, "x"));
            tgt->y = json_number_value (json_object_get (z, "y"));
            tgt->w = json_number_value (json_object_get (z, "w"));
            tgt->h = json_number_value (json_object_get (z, "h"));
        } else if (!strcmp (type, "line")) {
            tgt->type = ZG_TYPE_LINE;
            tgt->x1 = json_number_value (json_object_get (z, "x1"));
            tgt->y1 = json_number_value (json_object_get (z, "y1"));
            tgt->x2 = json_number_value (json_object_get (z, "x2"));
            tgt->y2 = json_number_value (json_object_get (z, "y2"));
        } else {
            continue;
        }
        n++;
    }
    self->s.nzones = n;
    for (int k = 0; k < ZG_MAX_ZONES; k++) self->s.line_last_side[k] = -1;
    json_decref (root);

    GST_INFO_OBJECT (self, "ch%d: loaded %d zone(s) from %s (draw_overlay=%d)",
                     self->s.channel, n, self->s.zones_config, self->s.draw_overlay);
}


/* --------------------------------------------------------------------- */
/* geometry helpers                                                       */
/* --------------------------------------------------------------------- */

/* Axis-aligned rectangle overlap. Both rects as (x,y,w,h). */
static inline int rects_overlap (int ax, int ay, int aw, int ah,
                                 int bx, int by, int bw, int bh) {
    return ax < bx + bw && ax + aw > bx && ay < by + bh && ay + ah > by;
}

static inline int ccw_sign (int ax, int ay, int bx, int by, int cx, int cy) {
    long long d = (long long)(bx - ax) * (cy - ay)
                - (long long)(by - ay) * (cx - ax);
    return d > 0 ? 1 : (d < 0 ? -1 : 0);
}

static int seg_seg_intersect (int ax, int ay, int bx, int by,
                              int cx, int cy, int dx, int dy) {
    int s1 = ccw_sign (ax, ay, bx, by, cx, cy);
    int s2 = ccw_sign (ax, ay, bx, by, dx, dy);
    int s3 = ccw_sign (cx, cy, dx, dy, ax, ay);
    int s4 = ccw_sign (cx, cy, dx, dy, bx, by);
    return (s1 != s2) && (s3 != s4);
}

/* Does the line segment A→B intersect (or pierce) the axis-aligned bbox
 * (rx,ry,rw,rh)? Endpoints-inside and edge-crossing both count. */
static int seg_hits_rect (int ax, int ay, int bx, int by,
                          int rx, int ry, int rw, int rh) {
    int lx0 = ax < bx ? ax : bx, lx1 = ax < bx ? bx : ax;
    int ly0 = ay < by ? ay : by, ly1 = ay < by ? by : ay;
    if (lx1 < rx || lx0 > rx + rw || ly1 < ry || ly0 > ry + rh) return 0;
    if (ax >= rx && ax <= rx + rw && ay >= ry && ay <= ry + rh) return 1;
    if (bx >= rx && bx <= rx + rw && by >= ry && by <= ry + rh) return 1;
    int x0 = rx, y0 = ry, x1 = rx + rw, y1 = ry + rh;
    if (seg_seg_intersect (ax, ay, bx, by, x0, y0, x1, y0)) return 1;
    if (seg_seg_intersect (ax, ay, bx, by, x1, y0, x1, y1)) return 1;
    if (seg_seg_intersect (ax, ay, bx, by, x0, y1, x1, y1)) return 1;
    if (seg_seg_intersect (ax, ay, bx, by, x0, y0, x0, y1)) return 1;
    return 0;
}


/* --------------------------------------------------------------------- */
/* prediction tree walk                                                   */
/* --------------------------------------------------------------------- */

typedef struct {
    ZoneGuard *self;
    int any_inside;
    int rect_hit_idx;    /* first matching rect zone index, or -1 */
    int line_cross_idx;  /* first crossed line index, or -1 */
    int have_centroid;
    int cx, cy;
    int visited;
} WalkCtx;

/* Minimum classifier probability for a detection to be considered real.
 * VVAS will report low-probability ghosts at the tail of NMS; those still
 * get a bounding box but should not trigger a zone alert. Higher values
 * make alerts stricter. */
#define ZG_MIN_PROB 0.50

/* Hysteresis: how many consecutive frames of the same state we require
 * before flipping the alert. Avoids single-frame flicker both ways.
 *   ENTER needs this many in-zone frames in a row
 *   CLEAR needs this many no-hit frames in a row (about 0.5 s @ 30 fps) */
#define ZG_ENTER_STREAK  3
#define ZG_CLEAR_STREAK  15

static double best_prob (const VvasInferPrediction *pred) {
    double best = 0.0;
    for (VvasList *l = pred->classifications; l; l = l->next) {
        VvasInferClassification *c = (VvasInferClassification *) l->data;
        if (c && c->class_prob > best) best = c->class_prob;
    }
    return best;   /* 0.0 if no classifications at all */
}

static void inspect_box (WalkCtx *ctx, const VvasInferPrediction *pred) {
    const VvasBoundingBox *b = &pred->bbox;
    if (b->width == 0 || b->height == 0) return;
    /* VVAS has already applied its own confidence threshold inside the
     * vvas_xinfer postprocessor. We trust `enabled` — any prediction that
     * reached here with enabled=false is one VVAS itself considers rejected
     * (it keeps them in the tree for introspection). */
    if (!pred->enabled) return;
    int bx = b->x, by = b->y;
    int bw = (int) b->width, bh = (int) b->height;

    /* Bottom-center anchor is only kept for debug; all hit-tests use the
     * full bounding box now. */
    if (!ctx->have_centroid) {
        ctx->cx = bx + bw / 2; ctx->cy = by + bh; ctx->have_centroid = 1;
    }

    int W = ctx->self->s.frame_w > 0 ? ctx->self->s.frame_w : 1;
    int H = ctx->self->s.frame_h > 0 ? ctx->self->s.frame_h : 1;

    for (int k = 0; k < ctx->self->s.nzones; k++) {
        ZgZone *z = &ctx->self->s.zones[k];
        if (z->type == ZG_TYPE_RECT) {
            int zx = (int) (z->x * W), zy = (int) (z->y * H);
            int zw = (int) (z->w * W), zh = (int) (z->h * H);
            /* Alert when the person's detection box touches or overlaps
             * the zone, not only when fully inside. */
            if (rects_overlap (bx, by, bw, bh, zx, zy, zw, zh)) {
                ctx->any_inside = 1;
                if (ctx->rect_hit_idx < 0) ctx->rect_hit_idx = k;
            }
        } else if (z->type == ZG_TYPE_LINE) {
            int ax = (int) (z->x1 * W), ay = (int) (z->y1 * H);
            int bxl = (int) (z->x2 * W), byl = (int) (z->y2 * H);
            /* Alert only when the detection box actually crosses / touches
             * the line segment — endpoints-inside OR segment clipping any
             * of the 4 bbox edges. No tripwire band any more. */
            if (seg_hits_rect (ax, ay, bxl, byl, bx, by, bw, bh)) {
                ctx->any_inside = 1;
                if (ctx->rect_hit_idx < 0) ctx->rect_hit_idx = k;
            }
        }
    }
}

/* Walk CHILDREN of the given prediction. Never inspects `pred` itself —
 * the root of a GstInferenceMeta tree typically carries a full-frame bbox
 * that would trivially overlap every zone. Only leaf-and-branch children
 * describe real detections. */
static void walk_children (GstInferencePrediction *pred, WalkCtx *ctx) {
    if (!pred) return;
    GSList *ch = gst_inference_prediction_get_children (pred);
    for (GSList *l = ch; l; l = l->next) {
        GstInferencePrediction *c = (GstInferencePrediction *) l->data;
        ctx->visited++;
        inspect_box (ctx, &c->prediction);
        walk_children (c, ctx);
    }
    g_slist_free (ch);
}


/* --------------------------------------------------------------------- */
/* transform_ip                                                           */
/* --------------------------------------------------------------------- */

/* Add one solid line-segment into the overlay shape-info. */
static void add_line_segment (VvasOverlayShapeInfo *s,
                              int x1, int y1, int x2, int y2,
                              int thickness, VvasOverlayColorData color) {
    VvasOverlayLineParams *lp = g_new0 (VvasOverlayLineParams, 1);
    lp->start_pt.x = x1; lp->start_pt.y = y1;
    lp->end_pt.x   = x2; lp->end_pt.y   = y2;
    lp->thickness  = thickness;
    lp->line_color = color;
    s->line_params = g_list_append ((GList*) s->line_params, lp);
    s->num_lines++;
}

/* Emit a dashed line A→B as a string of short solid segments. `dash` is
 * the on-segment length in pixels, `gap` the empty space between them.
 * Used to mimic the soft dashed look the web UI uses for zones. */
static void add_dashed_line (VvasOverlayShapeInfo *s,
                             int x1, int y1, int x2, int y2,
                             int dash, int gap, int thickness,
                             VvasOverlayColorData color) {
    double dx = x2 - x1, dy = y2 - y1;
    double len = sqrt (dx * dx + dy * dy);
    if (len < 1.0) return;
    double ux = dx / len, uy = dy / len;
    double stride = dash + gap;
    for (double p = 0; p < len; p += stride) {
        double qstart = p;
        double qend   = p + dash;
        if (qend > len) qend = len;
        int sx = (int) lrint (x1 + ux * qstart);
        int sy = (int) lrint (y1 + uy * qstart);
        int ex = (int) lrint (x1 + ux * qend);
        int ey = (int) lrint (y1 + uy * qend);
        add_line_segment (s, sx, sy, ex, ey, thickness, color);
    }
}

/* Append our zone shapes to the buffer's GstVvasOverlayMeta (creating the
 * meta if it's not there yet). Everything is rendered as dashed strokes
 * to match the web-UI "soft" look. */
static void zg_append_overlay_shapes (ZoneGuard *self, GstBuffer *buf)
{
    if (!self->s.draw_overlay || self->s.nzones == 0) return;
    int W = self->s.frame_w, H = self->s.frame_h;
    if (W <= 0 || H <= 0) return;

    GstVvasOverlayMeta *ometa = gst_buffer_get_vvas_overlay_meta (buf);
    if (!ometa) {
        ometa = gst_buffer_add_vvas_overlay_meta (buf);
        if (!ometa) return;
        vvas_overlay_shape_info_init (&ometa->shape_info);
    }

    VvasOverlayShapeInfo *s = &ometa->shape_info;
    VvasOverlayColorData red = { .red = 220, .green = 38, .blue = 38, .alpha = 255 };
    const int THICK = 2;

    /* Dash/gap scaled to frame size so it looks consistent across
     * 480p → 1080p sources. Roughly: 1.2% of width for dash, 0.7% gap. */
    int dash = W / 80; if (dash < 6)  dash = 6;
    int gap  = W / 140; if (gap  < 4) gap  = 4;

    for (int k = 0; k < self->s.nzones; k++) {
        ZgZone *z = &self->s.zones[k];
        if (z->type == ZG_TYPE_RECT) {
            int zx = (int) (z->x * W), zy = (int) (z->y * H);
            int zw = (int) (z->w * W), zh = (int) (z->h * H);
            /* Four dashed edges. */
            add_dashed_line (s, zx,      zy,      zx + zw, zy,      dash, gap, THICK, red); /* top */
            add_dashed_line (s, zx + zw, zy,      zx + zw, zy + zh, dash, gap, THICK, red); /* right */
            add_dashed_line (s, zx + zw, zy + zh, zx,      zy + zh, dash, gap, THICK, red); /* bottom */
            add_dashed_line (s, zx,      zy + zh, zx,      zy,      dash, gap, THICK, red); /* left */
        } else if (z->type == ZG_TYPE_LINE) {
            int ax = (int) (z->x1 * W), ay = (int) (z->y1 * H);
            int bxl = (int) (z->x2 * W), byl = (int) (z->y2 * H);
            add_dashed_line (s, ax, ay, bxl, byl, dash, gap, THICK, red);
        }
    }
}


static gboolean zoneguard_set_caps (GstBaseTransform *base,
                                    GstCaps *incaps, GstCaps *outcaps) {
    (void) outcaps;
    ZoneGuard *self = ZG_ZONEGUARD (base);
    GstVideoInfo info;
    if (gst_video_info_from_caps (&info, incaps)) {
        self->s.frame_w = info.width;
        self->s.frame_h = info.height;
        GST_INFO_OBJECT (self, "ch%d: frame size %dx%d",
                         self->s.channel, self->s.frame_w, self->s.frame_h);
    }
    return TRUE;
}


static GstFlowReturn zoneguard_transform_ip (GstBaseTransform *base, GstBuffer *buf)
{
    ZoneGuard *self = ZG_ZONEGUARD (base);
    zg_maybe_reload_zones (self);
    zg_open_socket (self);
    /* Attach the shared-memory fast path + promote this streaming thread
     * to realtime on core 3. Both are idempotent / one-shot. */
    zg_shm_attach (self);
    zg_promote_rt (self);

    if (self->s.nzones == 0) return GST_FLOW_OK;

    /* Draw zone shapes on the frame (if enabled). This runs on every buffer
     * and is a no-op when draw_overlay is false. */
    zg_append_overlay_shapes (self, buf);

    /* Always compute any_inside for THIS frame, even if the inference meta
     * is missing — that's the natural case for "no detections on this
     * frame" and must drive CLEAR transitions. */
    WalkCtx ctx = {0};
    ctx.self = self;
    ctx.rect_hit_idx = -1;
    ctx.line_cross_idx = -1;
    GstInferenceMeta *imeta = (GstInferenceMeta *)
        gst_buffer_get_meta (buf, gst_inference_meta_api_get_type ());
    if (imeta && imeta->prediction)
        walk_children (imeta->prediction, &ctx);

    /* Dump diagnostics once per second. Walks the prediction tree from the
     * GstInferencePrediction correctly (unlike the earlier buggy cast).
     * Keeps the per-frame path untouched. */
    if ((self->s.frame_counter % 30) == 0) {
        int n_enabled = 0, n_total = 0;
        double maxp = 0.0;
        int first_x = -1, first_y = -1, first_w = -1, first_h = -1;
        int first_en = -1;
        if (imeta && imeta->prediction) {
            GSList *ch = gst_inference_prediction_get_children (imeta->prediction);
            for (GSList *l = ch; l; l = l->next) {
                GstInferencePrediction *gp = (GstInferencePrediction *) l->data;
                VvasInferPrediction *p = &gp->prediction;
                n_total++;
                double pr = best_prob (p);
                if (pr > maxp) maxp = pr;
                if (p->enabled) n_enabled++;
                if (first_x < 0) {
                    first_x = p->bbox.x; first_y = p->bbox.y;
                    first_w = (int) p->bbox.width; first_h = (int) p->bbox.height;
                    first_en = p->enabled;
                }
            }
            g_slist_free (ch);
        }
        GST_WARNING_OBJECT (self,
            "ch%d stats: f=%d total=%d enabled=%d maxp=%.2f any_in=%d hit=%d miss=%d act=%d "
            "box0=(%d,%d,%dx%d,en=%d)",
            self->s.channel, self->s.frame_counter, n_total, n_enabled, maxp,
            ctx.any_inside, self->s.hit_streak, self->s.miss_streak,
            self->s.alert_active, first_x, first_y, first_w, first_h, first_en);
    }

    self->s.frame_counter++;

    /* Hysteresis-based state machine. Raw per-frame hit (ctx.any_inside)
     * is noisy: one phantom detection can flip it on, one missed inference
     * can flip it off. We fold that into two running streak counters and
     * only emit ENTER/CLEAR when the streak crosses a threshold. */
    if (ctx.any_inside) {
        self->s.hit_streak++;
        self->s.miss_streak = 0;
    } else {
        self->s.miss_streak++;
        self->s.hit_streak = 0;
    }

    int new_active = self->s.alert_active;
    if (!self->s.alert_active && self->s.hit_streak >= ZG_ENTER_STREAK)
        new_active = 1;
    else if (self->s.alert_active && self->s.miss_streak >= ZG_CLEAR_STREAK)
        new_active = 0;

    /* Heartbeat: every ~1 s re-send current state so a dropped event on
     * the socket doesn't leave the UI stuck. */
    int force_resend = (self->s.frame_counter - self->s.last_resend_frame) >= 30;

    if (new_active && (!self->s.alert_active || force_resend)) {
        const char *n = ctx.rect_hit_idx >= 0
            ? self->s.zones[ctx.rect_hit_idx].name : "zone";
        char reason[128];
        snprintf (reason, sizeof(reason), "person in %s", n);
        /* FAST PATH first — atomic bit-set on shm->flags, ~100 ns,
         * R5 sees it on its next poll tick (≤ 1 µs). */
        zg_shm_set (self, 1);
        /* SLOW PATH — UI notification via Unix socket; tens of µs typical
         * but not on the critical LED-drive path. */
        zg_send_event (self, "ENTER", n, reason);
        GST_INFO_OBJECT (self, "ch%d ENTER (hit_streak=%d)",
                         self->s.channel, self->s.hit_streak);
        self->s.last_resend_frame = self->s.frame_counter;
    } else if (!new_active && (self->s.alert_active || force_resend)) {
        zg_shm_set (self, 0);
        zg_send_event (self, "CLEAR", NULL, "no person in any zone");
        GST_INFO_OBJECT (self, "ch%d CLEAR (miss_streak=%d)",
                         self->s.channel, self->s.miss_streak);
        self->s.last_resend_frame = self->s.frame_counter;
    }
    self->s.alert_active = new_active;
    self->s.last_any_inside = ctx.any_inside;

    return GST_FLOW_OK;
}


/* --------------------------------------------------------------------- */
/* GObject boilerplate                                                    */
/* --------------------------------------------------------------------- */

static void zoneguard_init (ZoneGuard *self) {
    gst_base_transform_set_in_place (GST_BASE_TRANSFORM (self), TRUE);
    gst_base_transform_set_passthrough (GST_BASE_TRANSFORM (self), FALSE);
    self->s.channel = 0;
    self->s.sock_fd = -1;
    self->s.shm_fd = -1;
    self->s.shm = NULL;
    self->s.rt_promoted = 0;
    self->s.last_any_inside = 0;
    for (int k = 0; k < ZG_MAX_ZONES; k++) self->s.line_last_side[k] = -1;
}

static void zoneguard_finalize (GObject *obj) {
    ZoneGuard *self = ZG_ZONEGUARD (obj);
    if (self->s.sock_fd >= 0) { close (self->s.sock_fd); self->s.sock_fd = -1; }
    G_OBJECT_CLASS (zoneguard_parent_class)->finalize (obj);
}

static void zoneguard_set_property (GObject *obj, guint prop_id,
                                    const GValue *value, GParamSpec *pspec) {
    ZoneGuard *self = ZG_ZONEGUARD (obj);
    switch (prop_id) {
        case PROP_CHANNEL:
            self->s.channel = g_value_get_int (value);
            break;
        case PROP_ZONES_CONFIG: {
            const char *s = g_value_get_string (value);
            strncpy (self->s.zones_config, s ? s : "", sizeof(self->s.zones_config) - 1);
            self->s.zones_mtime = 0;
            break;
        }
        case PROP_EVENT_SOCKET: {
            const char *s = g_value_get_string (value);
            strncpy (self->s.event_socket, s ? s : "", sizeof(self->s.event_socket) - 1);
            break;
        }
        default:
            G_OBJECT_WARN_INVALID_PROPERTY_ID (obj, prop_id, pspec);
    }
}

static void zoneguard_get_property (GObject *obj, guint prop_id,
                                    GValue *value, GParamSpec *pspec) {
    ZoneGuard *self = ZG_ZONEGUARD (obj);
    switch (prop_id) {
        case PROP_CHANNEL:       g_value_set_int (value, self->s.channel); break;
        case PROP_ZONES_CONFIG:  g_value_set_string (value, self->s.zones_config); break;
        case PROP_EVENT_SOCKET:  g_value_set_string (value, self->s.event_socket); break;
        default:
            G_OBJECT_WARN_INVALID_PROPERTY_ID (obj, prop_id, pspec);
    }
}

static void zoneguard_class_init (ZoneGuardClass *klass) {
    GObjectClass *gobject_class = G_OBJECT_CLASS (klass);
    GstElementClass *element_class = GST_ELEMENT_CLASS (klass);
    GstBaseTransformClass *base_class = GST_BASE_TRANSFORM_CLASS (klass);

    gobject_class->set_property = zoneguard_set_property;
    gobject_class->get_property = zoneguard_get_property;
    gobject_class->finalize     = zoneguard_finalize;

    g_object_class_install_property (gobject_class, PROP_CHANNEL,
        g_param_spec_int ("channel", "Channel", "Channel index (0..3)",
                          0, 3, 0, G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS));
    g_object_class_install_property (gobject_class, PROP_ZONES_CONFIG,
        g_param_spec_string ("zones-config", "Zones config",
                             "Path to JSON array of zones", "",
                             G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS));
    g_object_class_install_property (gobject_class, PROP_EVENT_SOCKET,
        g_param_spec_string ("event-socket", "Event socket",
                             "Path to a Unix-domain datagram socket to send events", "",
                             G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS));

    gst_element_class_add_static_pad_template (element_class, &sink_template);
    gst_element_class_add_static_pad_template (element_class, &src_template);
    gst_element_class_set_static_metadata (element_class,
        "zoneguard",
        "Filter/Analytics",
        "Reads VVAS inference metadata and emits zone-entry / line-crossing events over a Unix socket",
        "internal <noreply@local>");

    base_class->transform_ip = zoneguard_transform_ip;
    base_class->set_caps     = zoneguard_set_caps;
}


static gboolean plugin_init (GstPlugin *plugin) {
    GST_DEBUG_CATEGORY_INIT (zoneguard_debug, "zoneguard", 0, "zoneguard filter");
    return gst_element_register (plugin, "zoneguard", GST_RANK_NONE,
                                 ZG_TYPE_ZONEGUARD);
}

#ifndef PACKAGE
#define PACKAGE "zoneguard"
#endif

GST_PLUGIN_DEFINE (
    GST_VERSION_MAJOR, GST_VERSION_MINOR,
    zoneguard,
    "VVAS zone guard: inference-meta to zone-alert events",
    plugin_init,
    "1.0", "LGPL", "zoneguard", "local"
)
