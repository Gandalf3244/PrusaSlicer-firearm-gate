/* gcheck - see gcheck.h. C99, no dynamic memory, libm only. */
#include "gcheck.h"

#include <math.h>
#include <string.h>

#include "fp_table.h"
typedef char gc_parts_fit[FP_N_PARTS <= GC_MAX_PARTS ? 1 : -1];     /* GC_MAX_PARTS too small for the table */
#ifdef GC_DEBUG
#include <stdio.h>
#endif

#define PI_F 3.14159265f
#define FIL_AREA (PI_F * 0.875f * 0.875f)   /* 1.75 mm filament */
#define CLOSE_TOL 0.6f                        /* x width: loop ends this close = closed */
#define CENTER_TOL 0.15f                      /* mm: same circle centre (concentric / next layer) */
#define TRACK_R_TOL 0.10f                     /* mm: same radius in the next sampled layer */
#define MIN_LAYERS 2                          /* sampled layers a cylinder must appear in */
#define ARC_STEP 0.1745f                      /* rad: arcs are split into <= 10 deg segments */
#define PARTNER_LO 0.6f                       /* x width: gap to the next perimeter of a wall family ... */
#define PARTNER_HI 1.4f                       /* ... (PARTNER_OFFSETS 0.9 / 1.35 in pipeline.gcode_mesh) */
#define GC_MAX_R 35.0f                        /* mm: largest circle radius considered */
#define COAX_TOL 0.5f                         /* mm: coaxial for the bore span (calibers._coaxial) */
/* straight edges and edge pairs */
#define RUN_COS 0.99863f                      /* cos 3 deg: segments continuing a straight run */
#define RUN_OFF 0.05f                         /* mm: largest offset of a run point from its line */
#define RUN_MIN 1.0f                          /* mm: shortest run kept */
#define PAIR_WIN (PI_F / GC_NBIN)            /* rad (2 deg): edges considered parallel; the bin width */
#define SIN_PAIR_WIN 0.034899497f             /* sin 2 deg */
#define PAIR_MIN_OVERLAP 1.0f                 /* mm */
#define PAIR_MAX_SEP 62.0f                    /* mm: widest slot / wall / horizontal hole */
#define PTRACK_COS 0.99985f                   /* cos 1 deg: the same edge pair in the next layer */
#define PTRACK_M_TOL 0.25f                    /* mm: its midline */
#define PTRACK_DRIFT 3.75f                    /* mm / mm: steepest midline drift followed (tilt <= 75 deg) */
#define PTRACK_SEP_TOL 0.10f                  /* mm: same separation, for a drifting pair's second layer */
#define TSLAB_MIN_K 0.05f                     /* mm / mm (3 deg): below, a face is vertical: fixed midline as before */
#define TSLAB_EST_DRIFT 0.2f                  /* mm: drift so far that makes a fixed track follow its drift */
#define TSLAB_MIN_DRIFT 0.3f                  /* mm: total midline drift that makes a slab tilted, not vertical */
#define TSLAB_M_RMS 0.05f                     /* mm: midline on a straight line against the height */
#define SLAB_SD 0.05f                         /* mm: constant separation (slab) ... */
#define SLAB_RANGE 0.20f                      /* ... and its range over the layers */
#define SLAB_MIN_AREA 20.0f                   /* mm^2: fingerprint.slab_elements min_area */
#define SLAB_D_MIN 1.0f                       /* mm: slab_elements d_range */
#define SLAB_D_MAX 60.0f
#define HCYL_R_MIN 0.75f                      /* mm: horizontal cylinder radius range */
#define HCYL_R_MAX 20.0f
/* tilted cylinders: ellipse loops */
#define ELL_MIN_N 10                          /* points of a loop fitted with an ellipse */
#define ELL_RATIO_MIN 1.01f                   /* a / b: rounder loops are circles (tilt < 8 deg) */
#define ELL_RATIO_MAX 4.0f                    /* tilt <= 75 deg; flatter is the edge pairs' business */
#define ELL_B_MIN 0.75f                       /* mm: smallest semi-minor axis */
#define ELL_DIR_SIN 0.105f                    /* sin 6 deg: same major-axis direction */
#define ELL_DIR_SIN_ROUND 0.34f               /* sin 20 deg: ... when the loop is nearly round (a / b < 1.1) */
#define ELL_MIN_LAYERS 4                      /* sampled layers a tilted cylinder must appear in ... */
#define ELL_MIN_DZ 0.8f                       /* mm: ... spanning this height */
#define ELL_TILT_TOL 0.07f                    /* rad (4 deg): tilt from the centre's drift = tilt from a / b */
#define ELL_CENTER_RMS 0.08f                  /* mm: centres on a straight line */
#define DEG(x) ((x) * 57.29578f)

/* ------------------------------------------------------------------ numbers */

static int parse_num(const char *p, const char *end, float *out, const char **next)
{
    int neg = 0, digits = 0;
    float v = 0.0f, scale = 1.0f;
    if (p < end && (*p == '-' || *p == '+')) { neg = (*p == '-'); p++; }
    while (p < end && *p >= '0' && *p <= '9') { v = v * 10.0f + (float)(*p - '0'); p++; digits++; }
    if (p < end && *p == '.') {
        p++;
        /* fractional digits accumulated as an integer: exact up to 7 digits */
        float frac = 0.0f;
        while (p < end && *p >= '0' && *p <= '9') {
            if (scale < 1e7f) { frac = frac * 10.0f + (float)(*p - '0'); scale *= 10.0f; }
            p++; digits++;
        }
        v += frac / scale;
    }
    *next = p;
    if (!digits) return 0;
    *out = neg ? -v : v;
    return 1;
}

static int starts_ci(const char *p, const char *end, const char *w)
{
    for (; *w; w++, p++) {
        if (p >= end) return 0;
        char c = *p;
        if (c >= 'A' && c <= 'Z') c = (char)(c - 'A' + 'a');
        if (c != *w) return 0;
    }
    return 1;
}

static int contains_ci(const char *p, const char *end, const char *w)
{
    for (; p < end; p++)
        if (starts_ci(p, end, w)) return 1;
    return 0;
}

/* ------------------------------------------------------------------ occupancy map */

/* 1 = material, 0 = empty, -1 = outside the map (unknown) */
static int map_get(const gc_state_t *s, int i, int j)
{
    const int N = s->cap.map_n;
    if (i < 0 || j < 0 || i >= N || j >= N) return -1;
    int k = j * N + i;
    return (s->map[k >> 3] >> (k & 7)) & 1;
}

static int map_at(const gc_state_t *s, float x, float y)
{
    return map_get(s, (int)floorf((x - s->map_x0) / GC_MAP_CELL), (int)floorf((y - s->map_y0) / GC_MAP_CELL));
}

static void map_segment(gc_state_t *s, float x0, float y0, float x1, float y1)
{
    /* cells along the segment, sampled every half cell by incremental stepping
       (no floor / division per sample: this runs for every millimetre extruded) */
    const float inv = 1.0f / GC_MAP_CELL, N = (float)s->cap.map_n;
    float fx = (x0 - s->map_x0) * inv, fy = (y0 - s->map_y0) * inv;
    float dx = (x1 - x0) * inv, dy = (y1 - y0) * inv;
    float L = sqrtf(dx * dx + dy * dy);
    int n = (int)(L * 2.0f) + 1;
    float sx = dx / n, sy = dy / n;
    for (int k = 0; k <= n; k++, fx += sx, fy += sy) {
        if (fx < 0 || fy < 0 || fx >= N || fy >= N) continue;
        int c = (int)fy * s->cap.map_n + (int)fx;
        s->map[c >> 3] |= (uint8_t)(1u << (c & 7));
    }
}

/* ------------------------------------------------------------------ paths, circles, straight runs */

static void path_point(gc_state_t *s, float x, float y)
{
    /* only a loop that can still be a usable circle keeps its points */
    if (!s->p_nocirc) {
        if (x < s->bx0) s->bx0 = x;
        if (x > s->bx1) s->bx1 = x;
        if (y < s->by0) s->by0 = y;
        if (y > s->by1) s->by1 = y;
        if (s->pn >= s->cap.path || s->bx1 - s->bx0 > 2 * GC_MAX_R + 2 || s->by1 - s->by0 > 2 * GC_MAX_R + 2)
            s->p_nocirc = 1;
        else {
            s->ppx[s->pn] = x - s->px0; s->ppy[s->pn] = y - s->py0;
        }
    }
    s->lastx = x; s->lasty = y;
    s->pn++;
}

static void flush_run(gc_state_t *s)
{
    if (!s->run_on) return;
    s->run_on = 0;
    float dx = s->rx1 - s->rx0, dy = s->ry1 - s->ry0, L = sqrtf(dx * dx + dy * dy);
    if (L < RUN_MIN) return;
    if (s->n_run >= s->cap.run) { s->res.overflow |= 8; return; }
    gc_run_t *r = &s->run[s->n_run++];
    r->mx = 0.5f * (s->rx0 + s->rx1); r->my = 0.5f * (s->ry0 + s->ry1);
    r->ux = dx / L; r->uy = dy / L; r->half = 0.5f * L;
    if (s->n_run > s->res.max_run) s->res.max_run = s->n_run;
}

static void run_segment(gc_state_t *s, float x0, float y0, float x1, float y1)
{
    float dx = x1 - x0, dy = y1 - y0, L = sqrtf(dx * dx + dy * dy);
    if (L < 1e-4f) return;
    float ux = dx / L, uy = dy / L;
    if (s->run_on && ux * s->rux + uy * s->ruy >= RUN_COS
        && fabsf((x1 - s->rx0) * -s->ruy + (y1 - s->ry0) * s->rux) <= RUN_OFF) {
        s->rx1 = x1; s->ry1 = y1;
        return;
    }
    flush_run(s);
    s->run_on = 1;
    s->rx0 = x0; s->ry0 = y0; s->rx1 = x1; s->ry1 = y1; s->rux = ux; s->ruy = uy;
}

static float layer_h(const gc_state_t *s)
{
    float h = s->layer_z - s->prev_z;
    if (!(h > 0.02f && h < 1.2f)) h = s->layer_z > 0.02f && s->layer_z < 1.2f ? s->layer_z : 0.2f;
    return h;
}

static int try_circle(gc_state_t *s, float w)
{
    /* Kasa fit in single precision on centred coordinates (the M4's FPU is
       single precision; centring keeps the moments small) */
    int n = (int)s->pn;
    float mx = 0, my = 0;
    for (int i = 0; i < n; i++) { mx += s->ppx[i]; my += s->ppy[i]; }
    mx /= n; my /= n;
    float sxx = 0, syy = 0, sxy = 0, sxz = 0, syz = 0, sz = 0, area2 = 0;
    for (int i = 0; i < n; i++) {
        float u = s->ppx[i] - mx, v = s->ppy[i] - my, z = u * u + v * v;
        sxx += u * u; syy += v * v; sxy += u * v; sxz += u * z; syz += v * z; sz += z;
        int j = i + 1 < n ? i + 1 : 0;                                /* shoelace, closed */
        area2 += s->ppx[i] * s->ppy[j] - s->ppx[j] * s->ppy[i];
    }
    /* centred: sum u = sum v = 0, so the system decouples: [sxx sxy; sxy syy] [D E] = -[sxz syz], F = -sz/n */
    float det = sxx * syy - sxy * sxy;
    if (fabsf(det) < 1e-9f) return 0;
    float D = (-sxz * syy + syz * sxy) / det, E = (-syz * sxx + sxz * sxy) / det, F = -sz / n;
    float a = -D / 2, b = -E / 2, r2 = a * a + b * b - F;
    if (!(r2 > 0)) return 0;
    float r = sqrtf(r2);
    if (r < 0.5f || r > GC_MAX_R) return 0;
    float e2 = 0;                                                     /* geometric residual */
    for (int i = 0; i < n; i++) {
        float u = s->ppx[i] - mx - a, v = s->ppy[i] - my - b, d = sqrtf(u * u + v * v) - r;
        e2 += d * d;
    }
    float rms = sqrtf(e2 / n);
    if (rms > 0.02f + 0.004f * r) return 0;
    /* one full turn around the centre: enclosed area ~ pi r^2 */
    float ratio = fabsf(area2) * 0.5f / (PI_F * r * r);
    if (ratio < 0.85f || ratio > 1.15f) return 0;
    s->res.circles++;
    if (s->n_circ >= s->cap.circ) { s->res.overflow |= 1; return 1; }
    gc_circ_t *c = &s->circ[s->n_circ++];
    c->cx = s->px0 + mx + a; c->cy = s->py0 + my + b; c->rc = r; c->w = w;
    if (s->n_circ > s->res.max_circ) s->res.max_circ = s->n_circ;
    return 1;
}

/* A loop around a tilted hole or boss is an ellipse: semi-minor axis = the radius
   (plus / minus half a bead), major axis along the tilt. Conic fit A x^2 + B xy + C y^2
   + D x + E y + F = 0 with A + C = 1 (the same in every pose) by linear least squares on
   centred, scaled points; Sampson distance for the residual. */
typedef struct { float A, B, C, D, E, F, xc, yc, am, bm, ux, uy; } conic_t;

static int conic_fit(const gc_state_t *s, int n, float mx, float my, float is, float sc, conic_t *c)
{
    float N[5][6];
    memset(N, 0, sizeof N);
    int m = 0;
    for (int i = 0; i < n; i++) {
        float u = (s->ppx[i] - mx) * is, v = (s->ppy[i] - my) * is;
        float p[5] = {u * u - v * v, u * v, u, v, 1.0f}, r = -v * v;
        for (int j = 0; j < 5; j++) {
            for (int k = 0; k <= j; k++) N[j][k] += p[j] * p[k];
            N[j][5] += p[j] * r;
        }
        m++;
    }
    for (int j = 0; j < 5; j++)
        for (int k = j + 1; k < 5; k++) N[j][k] = N[k][j];
    for (int col = 0; col < 5; col++) {                               /* Gauss, partial pivoting */
        int pv = col;
        for (int r = col + 1; r < 5; r++) if (fabsf(N[r][col]) > fabsf(N[pv][col])) pv = r;
        if (fabsf(N[pv][col]) < 1e-6f * m) return 0;
        if (pv != col) for (int k = 0; k < 6; k++) { float x = N[col][k]; N[col][k] = N[pv][k]; N[pv][k] = x; }
        for (int r = col + 1; r < 5; r++) {
            float f = N[r][col] / N[col][col];
            for (int k = col; k < 6; k++) N[r][k] -= f * N[col][k];
        }
    }
    float X[5];
    for (int col = 4; col >= 0; col--) {
        float acc = N[col][5];
        for (int k = col + 1; k < 5; k++) acc -= N[col][k] * X[k];
        X[col] = acc / N[col][col];
    }
    c->A = X[0]; c->B = X[1]; c->C = 1.0f - X[0]; c->D = X[2]; c->E = X[3]; c->F = X[4];
    float disc = 4 * c->A * c->C - c->B * c->B;
    if (disc <= 1e-4f) return 0;                                      /* not an ellipse */
    c->xc = (c->B * c->E - 2 * c->C * c->D) / disc; c->yc = (c->B * c->D - 2 * c->A * c->E) / disc;
    float k = -(c->F + 0.5f * (c->D * c->xc + c->E * c->yc));
    float hh = sqrtf(0.25f * (c->A - c->C) * (c->A - c->C) + 0.25f * c->B * c->B), lmax = 0.5f + hh, lmin = 0.5f - hh;
    if (!(k > 0) || !(lmin > 0)) return 0;
    c->bm = sqrtf(k / lmax) * sc; c->am = sqrtf(k / lmin) * sc;
    float th = 0.5f * atan2f(c->B, c->A - c->C);                      /* minor axis; major = th + 90 deg */
    c->ux = -sinf(th); c->uy = cosf(th);
    return 1;
}

static float conic_dist2(const conic_t *c, float u, float v)          /* Sampson distance^2, scaled units */
{
    float q = c->A * u * u + c->B * u * v + c->C * v * v + c->D * u + c->E * v + c->F;
    float gx = 2 * c->A * u + c->B * v + c->D, gy = c->B * u + 2 * c->C * v + c->E, g2 = gx * gx + gy * gy;
    return g2 < 1e-12f ? 1e9f : q * q / g2;
}

static void try_ellipse(gc_state_t *s, float w)
{
    int n = (int)s->pn;
    if (n < ELL_MIN_N) return;
    float mx = 0, my = 0;
    for (int i = 0; i < n; i++) { mx += s->ppx[i]; my += s->ppy[i]; }
    mx /= n; my /= n;
    float q2 = 0;
    for (int i = 0; i < n; i++) { float u = s->ppx[i] - mx, v = s->ppy[i] - my; q2 += u * u + v * v; }
    float sc = sqrtf(q2 / n);
    if (sc < ELL_B_MIN) return;
    float is = 1.0f / sc;
    conic_t c;
    if (!conic_fit(s, n, mx, my, is, sc, &c)) return;
    float e2 = 0, area2 = 0;
    for (int i = 0; i < n; i++) {
        e2 += conic_dist2(&c, (s->ppx[i] - mx) * is, (s->ppy[i] - my) * is);
        int j = i + 1 < n ? i + 1 : 0;
        area2 += s->ppx[i] * s->ppy[j] - s->ppx[j] * s->ppy[i];
    }
    float rms = sqrtf(e2 / n) * sc, tol = 0.02f + 0.004f * c.bm;
    float ratio = c.am / c.bm;
    float fill = fabsf(area2) * 0.5f / (PI_F * c.am * c.bm);          /* one turn around the centre */
#ifdef GC_DEBUG
    fprintf(stderr, "ell z %.2f n %d b %.3f ratio %.2f rms %.4f fill %.3f\n", s->layer_z, n, c.bm, ratio, rms, fill);
#endif
    if (rms > tol) return;
    if (ratio < ELL_RATIO_MIN || ratio > ELL_RATIO_MAX || c.bm < ELL_B_MIN || c.am > 2 * GC_MAX_R) return;
    if (fill < 0.85f || fill > 1.15f) return;
    s->res.ellipses++;
    if (s->n_ell >= s->cap.ell) { s->res.overflow |= 32; return; }
    gc_ell_t *e = &s->ell[s->n_ell++];
    e->cx = s->px0 + mx + c.xc * sc; e->cy = s->py0 + my + c.yc * sc; e->a = c.am; e->b = c.bm; e->ux = c.ux; e->uy = c.uy; e->w = w;
    if (s->n_ell > s->res.max_ell) s->res.max_ell = s->n_ell;
}

static void end_path(gc_state_t *s)
{
    if (!s->in_path) return;
    s->in_path = 0;
    flush_run(s);
    if (s->pn < 3 || s->plen < 2.0f) { s->n_run = s->path_run0; return; }
    float h = layer_h(s);
    float w = s->pe * FIL_AREA / (s->plen * h) + h * (1.0f - PI_F / 4.0f);
    if (!(w > 0.1f && w < 2.0f)) w = 0.45f;
    float gx = s->lastx - s->px0, gy = s->lasty - s->py0;
    if (sqrtf(gx * gx + gy * gy) > CLOSE_TOL * w) { s->n_run = s->path_run0; return; }   /* open path: no walls */
    s->res.loops++;
    int wb = (int)((w - 0.10f) * 100.0f);
    if ((s->outer || !s->has_roles) && wb >= 0 && wb < GC_WHIST && s->whist[wb] < 65535) s->whist[wb]++;
    if (s->pn < 6 || s->p_nocirc) return;
    if (!try_circle(s, w) && GC_TILT) try_ellipse(s, w);
}

static void path_segment(gc_state_t *s, float x0, float y0, float x1, float y1, float de)
{
    if (!s->in_path) {
        s->in_path = 1;
        s->px0 = x0; s->py0 = y0; s->plen = 0; s->pe = 0; s->pn = 0; s->p_nocirc = 0;
        s->bx0 = s->bx1 = x0; s->by0 = s->by1 = y0;
        s->run_on = 0; s->path_run0 = s->n_run;
        path_point(s, x0, y0);
    }
    float dx = x1 - x0, dy = y1 - y0;
    s->plen += sqrtf(dx * dx + dy * dy);
    s->pe += de;
    path_point(s, x1, y1);
    if (GC_EDGES && (s->outer || !s->has_roles)) run_segment(s, x0, y0, x1, y1);   /* labelled: outer walls only */
}

/* ------------------------------------------------------------------ elements */

static void add_elem(gc_state_t *s, uint8_t kind, float ax, float ay, float az, float cx, float cy, float cz,
                     float d, float L)
{
    if (s->n_elem >= s->cap.elem) { s->res.overflow |= 4; return; }
    gc_elem_t *e = &s->elem[s->n_elem++];
    e->kind = kind; e->a[0] = ax; e->a[1] = ay; e->a[2] = az; e->c[0] = cx; e->c[1] = cy; e->c[2] = cz;
    e->d = d; e->L = L; e->fill = L;
}

/* ------------------------------------------------------------------ vertical cylinders */

static void close_track(gc_state_t *s, int i)
{
    gc_track_t *t = &s->track[i];
    if (t->n >= MIN_LAYERS) {
        add_elem(s, t->kind, 0, 0, 1, t->cx, t->cy, 0.5f * (t->z0 + t->z1), 2.0f * t->sum_r / t->n, t->z1 - t->z0);
        s->res.v_cyl++;
    }
    s->track[i] = s->track[--s->n_track];
}

static void add_vcyl(gc_state_t *s, uint8_t kind, float cx, float cy, float R, float h)
{
    int best = -1;
    float bd = 1e9f;
    for (int i = 0; i < s->n_track; i++) {
        gc_track_t *t = &s->track[i];
        if (t->kind != kind || t->seen) continue;
        float dx = t->cx - cx, dy = t->cy - cy, dc = sqrtf(dx * dx + dy * dy);
        if (dc <= CENTER_TOL && fabsf(t->sum_r / t->n - R) <= TRACK_R_TOL && dc < bd) { bd = dc; best = i; }
    }
    if (best >= 0) {
        gc_track_t *t = &s->track[best];
        t->n++; t->sum_r += R; t->cx += (cx - t->cx) / t->n; t->cy += (cy - t->cy) / t->n;
        t->z1 = s->layer_z; t->seen = 1;
        return;
    }
    if (s->n_track >= s->cap.track) { s->res.overflow |= 2; return; }
    gc_track_t *t = &s->track[s->n_track++];
    t->cx = cx; t->cy = cy; t->sum_r = R; t->n = 1; t->kind = kind; t->seen = 1;
    t->z0 = s->layer_z - h; t->z1 = s->layer_z;
    if (s->n_track > s->res.max_track) s->res.max_track = s->n_track;
}

static float median_circ_w(const gc_state_t *s)
{
    /* bead width for the circles: median over the layer's circular loops (the walls
       of holes and bosses; Arachne widens beads but keeps the inset; the first
       layer is wider). A diameter moves one to one with it: TIGHT is 0.02 mm. */
    float lo = 0.1f, hi = 2.0f;
    int n = s->n_circ + s->n_ell;
    if (!n) return 0.45f;
    for (int it = 0; it < 24; it++) {                /* bisection on the value: no sort buffer */
        float mid = 0.5f * (lo + hi);
        int below = 0;
        for (int i = 0; i < s->n_circ; i++) below += s->circ[i].w < mid;
        for (int i = 0; i < s->n_ell; i++) below += s->ell[i].w < mid;
        if (below * 2 < n) lo = mid; else hi = mid;
    }
    return 0.5f * (lo + hi);
}

static float median_edge_w(const gc_state_t *s)
{
    /* bead width for the straight edges: median over the layer's outer-wall loops */
    uint32_t tot = 0, acc = 0;
    for (int i = 0; i < GC_WHIST; i++) tot += s->whist[i];
    if (!tot) return median_circ_w(s);
    for (int i = 0; i < GC_WHIST; i++) {
        acc += s->whist[i];
        if (2 * acc >= tot) return 0.10f + 0.01f * i + 0.005f;
    }
    return 0.45f;
}

static int region_empty(const gc_state_t *s, float cx, float cy, float rho)
{
    /* every map cell whose centre is within rho of (cx, cy), and the cell holding the centre */
    int ic = (int)floorf((cx - s->map_x0) / GC_MAP_CELL), jc = (int)floorf((cy - s->map_y0) / GC_MAP_CELL);
    if (map_get(s, ic, jc) != 0) return 0;
    if (rho <= 0) return 1;
    int k = (int)(rho / GC_MAP_CELL) + 1;
    for (int j = jc - k; j <= jc + k; j++)
        for (int i = ic - k; i <= ic + k; i++) {
            float x = s->map_x0 + (i + 0.5f) * GC_MAP_CELL - cx, y = s->map_y0 + (j + 0.5f) * GC_MAP_CELL - cy;
            if (x * x + y * y <= rho * rho && map_get(s, i, j) != 0) return 0;
        }
    return 1;
}

static int ring_empty(const gc_state_t *s, float cx, float cy, float r0, float r1)
{
    int ic = (int)floorf((cx - s->map_x0) / GC_MAP_CELL), jc = (int)floorf((cy - s->map_y0) / GC_MAP_CELL);
    int k = (int)(r1 / GC_MAP_CELL) + 1, tot = 0, occ = 0;
    for (int j = jc - k; j <= jc + k; j++)
        for (int i = ic - k; i <= ic + k; i++) {
            float x = s->map_x0 + (i + 0.5f) * GC_MAP_CELL - cx, y = s->map_y0 + (j + 0.5f) * GC_MAP_CELL - cy;
            float d2 = x * x + y * y;
            if (d2 < r0 * r0 || d2 > r1 * r1) continue;
            int m = map_get(s, i, j);
            if (m < 0) return 0;
            tot++; occ += m;
        }
    return tot > 0 && occ * 100 <= tot * 15;
}

static void layer_circles(gc_state_t *s, float w, float h)
{
    uint8_t *done = s->done;
    memset(done, 0, s->n_circ);
    for (int i = 0; i < s->n_circ; i++) {
        if (done[i]) continue;
        /* concentric family: innermost loop faces a hole, outermost may face air */
        int in = i, out = i;
        for (int j = i; j < s->n_circ; j++) {
            float dx = s->circ[j].cx - s->circ[i].cx, dy = s->circ[j].cy - s->circ[i].cy;
            if (dx * dx + dy * dy > CENTER_TOL * CENTER_TOL) continue;
            done[j] = 1;
            if (s->circ[j].rc < s->circ[in].rc) in = j;
            if (s->circ[j].rc > s->circ[out].rc) out = j;
        }
        /* without feature labels: a model wall is a family of perimeters one
           bead apart, a support shell or tree branch a single loop - the loop
           facing the hole (or the air) must have a concentric partner at ~one width */
        int in_ok = s->has_roles, out_ok = s->has_roles;     /* labelled support is already excluded */
        for (int j = i; j < s->n_circ; j++) {
            float dx = s->circ[j].cx - s->circ[i].cx, dy = s->circ[j].cy - s->circ[i].cy;
            if (dx * dx + dy * dy > CENTER_TOL * CENTER_TOL) continue;
            float gi = s->circ[j].rc - s->circ[in].rc, go = s->circ[out].rc - s->circ[j].rc;
            if (gi >= PARTNER_LO * w && gi <= PARTNER_HI * w) in_ok = 1;
            if (go >= PARTNER_LO * w && go <= PARTNER_HI * w) out_ok = 1;
        }
        const gc_circ_t *ci = &s->circ[in], *co = &s->circ[out];
        float Rh = ci->rc - 0.5f * w;
        if (in_ok && Rh >= 0.5f && region_empty(s, ci->cx, ci->cy, Rh - 0.75f))
            add_vcyl(s, GC_HOLE, ci->cx, ci->cy, Rh, h);
        float Rb = co->rc + 0.5f * w;
        if (out_ok && ring_empty(s, co->cx, co->cy, Rb + 0.75f, Rb + 2.0f))
            add_vcyl(s, GC_BOSS, co->cx, co->cy, Rb, h);
    }
    for (int i = s->n_track - 1; i >= 0; i--) {
        if (!s->track[i].seen) close_track(s, i);
        else s->track[i].seen = 0;
    }
}

/* ------------------------------------------------------------------ tilted cylinders */

static void ell_dir(const gc_etrack_t *t, float *ux, float *uy)
{
    float th = 0.5f * atan2f(t->s2, t->c2);                            /* mean of an axis (u = -u) */
    *ux = cosf(th); *uy = sinf(th);
}

static void close_etrack(gc_state_t *s, int i)
{
    gc_etrack_t *t = &s->etrack[i];
    if (t->n >= ELL_MIN_LAYERS && t->lz - t->zref >= ELL_MIN_DZ) {
        /* centre against height: a straight line whose slope is the tilt */
        double n = t->n, den = n * t->szz - t->sz * t->sz;
        if (den > 1e-9) {
            double vx = (n * t->szx - t->sz * t->sx) / den, bx = (t->sx - vx * t->sz) / n;
            double vy = (n * t->szy - t->sz * t->sy) / den, by = (t->sy - vy * t->sz) / n;
            double ex = t->sxx - 2 * vx * t->szx - 2 * bx * t->sx + vx * vx * t->szz + 2 * vx * bx * t->sz + n * bx * bx;
            double ey = t->syy - 2 * vy * t->szy - 2 * by * t->sy + vy * vy * t->szz + 2 * vy * by * t->sz + n * by * by;
            float rms = (float)sqrt((ex + ey > 0 ? ex + ey : 0) / n);
            float tan_d = (float)sqrt(vx * vx + vy * vy), ratio = t->sum_ratio / t->n;
            float tan_s = sqrtf(fmaxf(ratio * ratio - 1.0f, 0.0f));
            float ux, uy;
            ell_dir(t, &ux, &uy);
            int dir_ok = tan_d < 0.1f || fabsf((float)vx * uy - (float)vy * ux) <= ELL_DIR_SIN * tan_d;
#ifdef GC_DEBUG
            fprintf(stderr, "etrack k %d n %u dz %.2f b %.3f ratio %.3f tilt_d %.1f tilt_s %.1f rms %.3f dir %d\n", t->kind, t->n,
                    t->lz - t->zref, t->sum_b / t->n, ratio, DEG(atanf(tan_d)), DEG(atanf(tan_s)), rms, dir_ok);
#endif
            if (rms <= ELL_CENTER_RMS && dir_ok && fabsf(atanf(tan_d) - atanf(tan_s)) <= ELL_TILT_TOL) {
                float ca = 1.0f / sqrtf(1.0f + tan_d * tan_d), r = t->sum_b / t->n;
                float zm = 0.5f * (t->z0 + t->z1) - t->zref;
                /* complete sections span L cos(tilt) - 2 r sin(tilt) of height */
                float L = (t->z1 - t->z0) / ca + 2.0f * r * tan_d;
                add_elem(s, t->kind, (float)vx * ca, (float)vy * ca, ca, t->x0 + (float)(bx + vx * (double)zm),
                         t->y0 + (float)(by + vy * (double)zm), t->zref + zm, 2.0f * r, L);
                s->res.t_cyl++;
            }
        }
    }
    s->etrack[i] = s->etrack[--s->n_etrack];
}

/* a hole / boss section of semi-axes a >= b (the cylinder's radius), major axis (ux, uy) */
static void add_ecyl(gc_state_t *s, uint8_t kind, float cx, float cy, float a, float b, float ux, float uy, float h)
{
    float ratio = a / b, tan_s = sqrtf(fmaxf(ratio * ratio - 1.0f, 0.0f)), z = s->layer_z;
    int best = -1;
    float bd = 1e9f, bsx = 0, bsy = 0;
    for (int i = 0; i < s->n_etrack; i++) {
        gc_etrack_t *t = &s->etrack[i];
        if (t->kind != kind || t->seen) continue;
        if (fabsf(t->sum_b / t->n - b) > TRACK_R_TOL) continue;
        float rt = t->sum_ratio / t->n;
        if (fabsf(ratio - rt) > 0.05f * rt + 0.02f) continue;
        float tux, tuy;
        ell_dir(t, &tux, &tuy);
        if (fabsf(ux * tuy - uy * tux) > (rt < 1.1f ? ELL_DIR_SIN_ROUND : ELL_DIR_SIN)) continue;
        float dz = z - t->lz, m = sqrtf(fmaxf(rt * rt - 1.0f, 0.0f));
        float px, py, tol, sx = 0, sy = 0;
        if (t->n == 1) {                                               /* drift sign still unknown */
            float d1x = cx - (t->lx + m * dz * tux), d1y = cy - (t->ly + m * dz * tuy);
            float d2x = cx - (t->lx - m * dz * tux), d2y = cy - (t->ly - m * dz * tuy);
            int pos = d1x * d1x + d1y * d1y <= d2x * d2x + d2y * d2y;
            sx = (pos ? m : -m) * tux; sy = (pos ? m : -m) * tuy;
            px = t->lx + sx * dz; py = t->ly + sy * dz;
            tol = CENTER_TOL + 0.3f * m * dz;
        } else {
            px = t->lx + t->vx * dz; py = t->ly + t->vy * dz;
            tol = CENTER_TOL + 0.2f * m * dz;
        }
        float dx = cx - px, dy = cy - py, dc = sqrtf(dx * dx + dy * dy);
        if (dc <= tol && dc < bd) { bd = dc; best = i; bsx = sx; bsy = sy; }
    }
    if (best >= 0) {
        gc_etrack_t *t = &s->etrack[best];
        double zz = z - t->zref, xx = cx - t->x0, yy = cy - t->y0;
        t->n++;
        t->sz += zz; t->szz += zz * zz; t->sx += xx; t->szx += zz * xx; t->sy += yy; t->szy += zz * yy;
        t->sxx += xx * xx; t->syy += yy * yy;
        t->sum_b += b; t->sum_ratio += ratio; t->c2 += ux * ux - uy * uy; t->s2 += 2 * ux * uy;
        if (t->n == 2) { t->vx = bsx; t->vy = bsy; }
        if (t->n >= ELL_MIN_LAYERS && zz >= (double)ELL_MIN_DZ) {             /* long enough: the fitted slope */
            double n = t->n, den = n * t->szz - t->sz * t->sz;
            if (den > 1e-9) {
                t->vx = (float)((n * t->szx - t->sz * t->sx) / den);
                t->vy = (float)((n * t->szy - t->sz * t->sy) / den);
            }
        }
        t->lx = cx; t->ly = cy; t->lz = z; t->z1 = z; t->seen = 1;
        return;
    }
    (void)tan_s;
    if (s->n_etrack >= s->cap.etrack) { s->res.overflow |= 64; return; }
    gc_etrack_t *t = &s->etrack[s->n_etrack++];
    memset(t, 0, sizeof *t);
    t->x0 = t->lx = cx; t->y0 = t->ly = cy; t->zref = t->lz = z; t->z0 = z - h; t->z1 = z;
    t->n = 1; t->kind = kind; t->seen = 1;
    t->sum_b = b; t->sum_ratio = ratio; t->c2 = ux * ux - uy * uy; t->s2 = 2 * ux * uy;
    if (s->n_etrack > s->res.max_etrack) s->res.max_etrack = s->n_etrack;
}

/* the ring just outside an ellipse (semi-axes a, b, major axis u) holds almost no material */
static int ell_ring_empty(const gc_state_t *s, float cx, float cy, float a, float b, float ux, float uy)
{
    static const float off[3] = {1.0f, 1.5f, 2.0f};
    int tot = 0, occ = 0;
    for (int k = 0; k < 3; k++)
        for (int j = 0; j < 48; j++) {
            float t = j * (2 * PI_F / 48), ca = cosf(t) * (a + off[k]), sb = sinf(t) * (b + off[k]);
            int m = map_at(s, cx + ca * ux - sb * uy, cy + ca * uy + sb * ux);
            if (m < 0) return 0;
            tot++; occ += m;
        }
    return occ * 100 <= tot * 15;
}

static void layer_ellipses(gc_state_t *s, float w, float h)
{
    uint8_t *done = s->edone;
    memset(done, 0, s->n_ell);
    for (int i = 0; i < s->n_ell; i++) {
        if (done[i]) continue;
        const gc_ell_t *ei = &s->ell[i];
        int in = i, out = i;
        uint8_t *fam = s->fam;
        memset(fam, 0, s->n_ell);
        for (int j = i; j < s->n_ell; j++) {                          /* concentric, same major axis */
            const gc_ell_t *ej = &s->ell[j];
            float dx = ej->cx - ei->cx, dy = ej->cy - ei->cy;
            if (dx * dx + dy * dy > CENTER_TOL * CENTER_TOL) continue;
            if (fabsf(ej->ux * ei->uy - ej->uy * ei->ux) > (ei->a < 1.1f * ei->b ? ELL_DIR_SIN_ROUND : ELL_DIR_SIN)) continue;
            done[j] = fam[j] = 1;
            if (ej->b < s->ell[in].b) in = j;
            if (ej->b > s->ell[out].b) out = j;
        }
        int in_ok = s->has_roles, out_ok = s->has_roles;
        for (int j = i; j < s->n_ell; j++) {
            if (!fam[j]) continue;
            float gi = s->ell[j].b - s->ell[in].b, go = s->ell[out].b - s->ell[j].b;
            if (gi >= PARTNER_LO * w && gi <= PARTNER_HI * w) in_ok = 1;
            if (go >= PARTNER_LO * w && go <= PARTNER_HI * w) out_ok = 1;
        }
        const gc_ell_t *ci = &s->ell[in], *co = &s->ell[out];
        float bh = ci->b - 0.5f * w, ah = ci->a - 0.5f * w;
#ifdef GC_DEBUG
        fprintf(stderr, "efam z %.2f b_in %.3f ratio %.2f in_ok %d empty %d | b_out %.3f out_ok %d ring %d\n", s->layer_z, ci->b - 0.5f * w,
                (ci->a - 0.5f * w) / (ci->b - 0.5f * w), in_ok, region_empty(s, ci->cx, ci->cy, ci->b - 0.5f * w - 0.75f),
                co->b + 0.5f * w, out_ok, ell_ring_empty(s, co->cx, co->cy, co->a + 0.5f * w, co->b + 0.5f * w, co->ux, co->uy));
#endif
        if (in_ok && bh >= 0.5f && region_empty(s, ci->cx, ci->cy, bh - 0.75f)) {
            float q = 0.5f * ah;                                       /* along the major axis too (0.87 bh from the wall) */
            if (q <= bh - 0.75f || (map_at(s, ci->cx + q * ci->ux, ci->cy + q * ci->uy) == 0
                                    && map_at(s, ci->cx - q * ci->ux, ci->cy - q * ci->uy) == 0))
                add_ecyl(s, GC_HOLE, ci->cx, ci->cy, ah, bh, ci->ux, ci->uy, h);
        }
        float bb = co->b + 0.5f * w, ab = co->a + 0.5f * w;
        if (out_ok && ell_ring_empty(s, co->cx, co->cy, ab, bb, co->ux, co->uy))
            add_ecyl(s, GC_BOSS, co->cx, co->cy, ab, bb, co->ux, co->uy, h);
    }
    for (int i = s->n_etrack - 1; i >= 0; i--) {
        if (!s->etrack[i].seen) close_etrack(s, i);
        else s->etrack[i].seen = 0;
    }
}

/* ------------------------------------------------------------------ edge pairs: slabs and horizontal cylinders */

/* a slab of this kind, width and size that some fingerprint element could take (LOOSE band) */
static int slab_useful(uint8_t kind, float d, float L)
{
    const float (*t)[2] = kind == GC_WALL ? FP_WALL_D : FP_SLOT_D;
    int n = kind == GC_WALL ? FP_N_WALL_D : FP_N_SLOT_D;
    int lo = 0, hi = n;
    while (lo < hi) { int m = (lo + hi) / 2; if (t[m][0] < d - BAND_LOOSE_D) lo = m + 1; else hi = m; }
    for (int k = lo; k < n && t[k][0] <= d + BAND_LOOSE_D; k++)
        if (t[k][1] <= 0 || L >= t[k][1] - BAND_LOOSE_LEN) return 1;
    return 0;
}
static void close_ptrack(gc_state_t *s, int i)
{
    gc_ptrack_t *t = &s->ptrack[i];
    float nx = -t->uy, ny = t->ux;
    float tm = 0.5f * (t->t0 + t->t1);
    float px = t->ux * tm + nx * t->m, py = t->uy * tm + ny * t->m;     /* on the midline, mid overlap */
    double n = t->n;
    if (t->drift && t->n >= 3 && fabsf(t->k) >= TSLAB_MIN_K && fabsf(t->k) * (t->z1 - t->z0) >= TSLAB_MIN_DRIFT) {
        /* tilted face pair: midline m = m0 + k z', separation constant; the faces' normal is
           (n, -k) / sqrt(1 + k^2) and their distance the horizontal separation / sqrt(1 + k^2) */
        double den = n * t->fzz - t->fz * t->fz;
        if (t->n >= 4 && den > 1e-9) {
            double k = (n * t->fzm - t->fz * t->fm) / den, m0 = (t->fm - k * t->fz) / n;
            double em = t->fmm - 2 * k * t->fzm - 2 * m0 * t->fm + k * k * t->fzz + 2 * k * m0 * t->fz + n * m0 * m0;
            float rms_m = (float)sqrt(em > 0 ? em / n : 0);
            double mean = t->s_sum / n, var = t->s_sq / n - mean * mean;
            float sd = (float)sqrt(var > 0 ? var : 0), range = t->s_max - t->s_min;
            float c = 1.0f / sqrtf(1.0f + (float)(k * k)), d = (float)mean * c;
            float slope_len = (t->z1 - t->z0) / c, L = fminf(t->t1 - t->t0, slope_len);
            if (rms_m <= TSLAB_M_RMS && sd <= SLAB_SD && range <= SLAB_RANGE && (t->t1 - t->t0) * slope_len >= SLAB_MIN_AREA
                && d >= SLAB_D_MIN && d <= SLAB_D_MAX && slab_useful(t->kind, d, L)) {
                float zm = 0.5f * (t->z0 + t->z1), mm = (float)(m0 + k * (double)(zm - t->zref));
                add_elem(s, t->kind, nx * c, ny * c, -(float)k * c, t->ux * tm + nx * mm, t->uy * tm + ny * mm, zm, d, L);
                s->res.t_slabs++;
            }
        }
    } else if (t->n >= 3) {
        double mean = t->s_sum / n, var = t->s_sq / n - mean * mean;
        float sd = (float)sqrt(var > 0 ? var : 0), range = t->s_max - t->s_min;
        float area = (t->t1 - t->t0) * (t->z1 - t->z0);
        if (sd <= SLAB_SD && range <= SLAB_RANGE) {
            /* constant separation: two vertical parallel faces */
            float L = fminf(t->t1 - t->t0, t->z1 - t->z0);
            if (area >= SLAB_MIN_AREA && mean >= (double)SLAB_D_MIN && mean <= (double)SLAB_D_MAX && slab_useful(t->kind, (float)mean, L)) {
                add_elem(s, t->kind, nx, ny, 0, px, py, 0.5f * (t->z0 + t->z1), (float)mean, L);
                s->res.slabs++;
            }
        } else if (t->n >= 4) {
            /* the separation follows a circle's chord: y = s^2/4 + z'^2 = 2 zc' z' + (R^2 - zc'^2) */
            double den = n * t->fzz - t->fz * t->fz;
            if (den > 1e-9) {
                double a = (n * t->fzy - t->fz * t->fy) / den, b = (t->fy - a * t->fz) / n;
                double zc = a / 2, R2 = b + zc * zc;
                if (R2 > 0) {
                    float R = (float)sqrt(R2);
                    double e2 = t->fyy - 2 * a * t->fzy - 2 * b * t->fy + a * a * t->fzz + 2 * a * b * t->fz + n * b * b;
                    float rms = (float)sqrt(e2 > 0 ? e2 / n : 0);
                    if (R >= HCYL_R_MIN && R <= HCYL_R_MAX && rms <= 0.08f * R + 0.03f
                        && t->z1 - t->z0 >= R && t->s_max >= 1.2f * R) {
                        add_elem(s, t->kind == GC_SLOT ? GC_HOLE : GC_BOSS, t->ux, t->uy, 0, px, py,
                                 t->zref + (float)zc, 2 * R, t->t1 - t->t0);
                        s->res.h_cyl++;
                    }
                }
            }
        }
    }
    s->ptrack[i] = s->ptrack[--s->n_ptrack];
}

/* phase 0: join a track whose (predicted) midline it continues, else return 0; phase 1:
   join a one-layer track as a drifting (tilted) face, else start a track. Two phases so a
   drifting match cannot take a track that another pair of the layer continues exactly. */
static int add_pair(gc_state_t *s, uint8_t kind, float ux, float uy, float m, float t0, float t1, float sep, float h,
                    int phase)
{
    int best = -1, bdrift = 0;
    float bd = 1e9f;
    for (int i = 0; i < s->n_ptrack; i++) {
        gc_ptrack_t *t = &s->ptrack[i];
        if (t->kind != kind || t->seen || ux * t->ux + uy * t->uy < PTRACK_COS) continue;
        if (!(t0 < t->t1 && t1 > t->t0)) continue;
        float dz = s->layer_z - t->lz, dd = fabsf(m - (t->lm + t->k * dz));
        float dm = t->drift ? dd : fabsf(m - t->m);
        if (!phase) {
            if (dm <= PTRACK_M_TOL && dm < bd) { bd = dm; best = i; bdrift = t->drift; }
            /* a slowly tilted face first joins as a fixed one; once its drift is clear, follow it */
            else if (!t->drift && dd <= PTRACK_M_TOL && dd < bd && fabsf(t->k) >= TSLAB_MIN_K
                     && fabsf(t->k) * (t->lz - t->z0) >= TSLAB_EST_DRIFT) { bd = dd; best = i; bdrift = 1; }
        } else if (t->n == 1 && fabsf(sep - t->ls) <= PTRACK_SEP_TOL && dm <= PTRACK_M_TOL + PTRACK_DRIFT * dz && dm < bd) {
            bd = dm; best = i; bdrift = 1;                           /* drift not known yet: a tilted face */
        }
    }
    if (!phase && best < 0) return 0;
    s->res.pairs++;
    gc_ptrack_t *t;
    if (best >= 0) {
        t = &s->ptrack[best];
        if (bdrift) t->drift = 1;
    } else {
        if (s->n_ptrack >= s->cap.ptrack) { s->res.overflow |= 16; return 1; }
        t = &s->ptrack[s->n_ptrack++];
        memset(t, 0, sizeof *t);
        t->ux = ux; t->uy = uy; t->m = t->lm = m; t->t0 = t0; t->t1 = t1; t->kind = kind;
        t->zref = s->layer_z - 0.5f * h; t->z0 = s->layer_z - h; t->z1 = s->layer_z;
        t->s_min = t->s_max = sep;
        if (s->n_ptrack > s->res.max_ptrack) s->res.max_ptrack = s->n_ptrack;
    }
    t->seen = 1;
    if (t0 < t->t0) t->t0 = t0;
    if (t1 > t->t1) t->t1 = t1;
    t->z1 = s->layer_z;
    if (sep < t->s_min) t->s_min = sep;
    if (sep > t->s_max) t->s_max = sep;
    double z = (double)(s->layer_z - 0.5f * h - t->zref), y = (double)sep * (double)sep / 4 + z * z;   /* slice plane: mid-layer */
    t->n++;
    t->s_sum += (double)sep; t->s_sq += (double)sep * (double)sep;
    t->fz += z; t->fzz += z * z; t->fy += y; t->fzy += z * y; t->fyy += y * y;
    t->fm += (double)m; t->fzm += z * (double)m; t->fmm += (double)m * (double)m;
    if (t->n >= 2) {                                                  /* midline drift: the face's tilt */
        double den = t->n * t->fzz - t->fz * t->fz;
        if (den > 1e-9) t->k = (float)((t->n * t->fzm - t->fz * t->fm) / den);
    }
    t->lm = m; t->lz = s->layer_z; t->ls = sep;
    return 1;
}

static int all_empty(const gc_state_t *s, const gc_run_t *r, float nx, float ny, float o)
{
    for (int k = -1; k <= 1; k++) {
        float t = 0.6f * r->half * k;
        if (map_at(s, r->mx + r->ux * t + nx * o, r->my + r->uy * t + ny * o) != 0) return 0;
    }
    return 1;
}

static float bin_nx(int b) { return -sinf((b + 0.5f) * PAIR_WIN); }
static float bin_ny(int b) { return cosf((b + 0.5f) * PAIR_WIN); }

/* Walk the edges of direction bin `bin` from edge ia's offset outward (toward +n of
   edge ia when `toward` = +1, -n when -1). mode 0: family partner perimeter at ~one
   width; 1: the first overlapping edge (gap side: nothing may lie between); 2: the
   first overlapping boundary edge (material side: skip the part's own inner
   perimeters). Returns the edge index (q = its offset along ia's normal) or -1. */
static int scan_bin(const gc_state_t *s, int ia, int bin, int toward, int mode, float w, float *q_out)
{
    const gc_run_t *a = &s->run[ia];
    float nbx = bin_nx(bin), nby = bin_ny(bin);
    float nx = -a->uy, ny = a->ux;
    int dir = toward * (nx * nbx + ny * nby >= 0 ? 1 : -1);          /* offsets grow along the bin normal */
    float oa = a->mx * nbx + a->my * nby;
    int lo = s->bin_start[bin], hi = s->bin_start[bin + 1];
    int L = lo, H = hi;
    while (L < H) { int M = (L + H) / 2; if (s->run[s->run_order[M]].off < oa) L = M + 1; else H = M; }
    for (int k = dir > 0 ? L : L - 1; k >= lo && k < hi; k += dir) {
        int j = s->run_order[k];
        if (j == ia) continue;
        const gc_run_t *b = &s->run[j];
        if (fabsf(b->off - oa) > PAIR_MAX_SEP + 2.0f) break;
        if (fabsf(a->ux * b->uy - a->uy * b->ux) > SIN_PAIR_WIN) continue;   /* sin of the angle between the lines */
        float q = (b->mx - a->mx) * nx + (b->my - a->my) * ny;
        if (q * toward <= 0) continue;                                 /* ordering noise: wrong side */
        float tb = (b->mx - a->mx) * a->ux + (b->my - a->my) * a->uy;
        float ov = fminf(a->half, tb + b->half) - fmaxf(-a->half, tb - b->half);
        if (mode == 0) {
            if (fabsf(q) > PARTNER_HI * w) break;
            if (ov > 0.5f && fabsf(q) >= PARTNER_LO * w) { *q_out = q; return j; }
            continue;
        }
        if (fabsf(q) > PAIR_MAX_SEP) break;
        if (ov < PAIR_MIN_OVERLAP || fabsf(q) < 0.3f) continue;
        if (mode == 1 || b->side) { *q_out = q; return j; }
    }
    return -1;
}

/* the nearest result of scan_bin over the edge's bin and its two neighbours */
static int scan(const gc_state_t *s, int ia, int toward, int mode, float w, float *q_out)
{
    int best = -1, b0 = s->run[ia].bin;
    float bq = 1e9f;
    for (int d = -1; d <= 1; d++) {
        float q;
        int j = scan_bin(s, ia, (b0 + d + GC_NBIN) % GC_NBIN, toward, mode, w, &q);
        if (j >= 0 && fabsf(q) < bq) { bq = fabsf(q); best = j; *q_out = q; }
    }
    return best;
}

static void layer_edges(gc_state_t *s, float w, float h)
{
    int n = s->n_run;
    float o = 0.5f * w + 0.8f;
    uint16_t *cnt = s->cnt;
    memset(cnt, 0, sizeof s->cnt);
    /* which side of each edge is empty (the edge is the part's boundary there), its direction bin */
    for (int i = 0; i < n; i++) {
        gc_run_t *r = &s->run[i];
        float nx = -r->uy, ny = r->ux;
        int pe = all_empty(s, r, nx, ny, o), ne = all_empty(s, r, -nx, -ny, o);
        r->side = (int8_t)(pe && !ne ? 1 : (ne && !pe ? -1 : 0));
        float th = atan2f(r->uy, r->ux);
        if (th < 0) th += PI_F;
        if (th >= PI_F) th -= PI_F;
        int b = (int)(th / PAIR_WIN);
        r->bin = (uint8_t)(b < 0 ? 0 : (b >= GC_NBIN ? GC_NBIN - 1 : b));
        r->off = r->mx * bin_nx(r->bin) + r->my * bin_ny(r->bin);
        r->fam = (uint8_t)s->has_roles;
        r->near_gap = r->near_mat = -1;
        cnt[r->bin]++;
    }
    /* order by (bin, offset): counting sort on the bin, insertion sort on the offset within it */
    s->bin_start[0] = 0;
    for (int b = 0; b < GC_NBIN; b++) s->bin_start[b + 1] = (uint16_t)(s->bin_start[b] + cnt[b]);
    memset(cnt, 0, sizeof s->cnt);
    for (int i = 0; i < n; i++) {
        int b = s->run[i].bin, lo = s->bin_start[b], k = lo + cnt[b]++;
        while (k > lo && s->run[s->run_order[k - 1]].off > s->run[i].off) { s->run_order[k] = s->run_order[k - 1]; k--; }
        s->run_order[k] = (uint16_t)i;
    }
    float q;
    if (!s->has_roles)
        /* without labels: a model edge has a parallel partner perimeter one bead inside it */
        for (int i = 0; i < n; i++)
            if (s->run[i].side && scan(s, i, -s->run[i].side, 0, w, &q) >= 0) s->run[i].fam = 1;
    /* nearest facing edge across the gap, nearest boundary edge across the material */
    for (int i = 0; i < n; i++) {
        gc_run_t *a = &s->run[i];
        if (!a->side || !a->fam) continue;
        float nx = -a->uy, ny = a->ux;
        for (int k = 0; k < 2; k++) {
            int j = scan(s, i, k ? -a->side : a->side, k ? 2 : 1, w, &q);
            if (j < 0) continue;
            const gc_run_t *b = &s->run[j];
            if (!b->side || !b->fam) continue;
            float qb = (a->mx - b->mx) * -b->uy + (a->my - b->my) * b->ux;
            int b_to_a = (qb > 0 ? 1 : -1) == b->side;                     /* b's empty side faces a */
            float tb = (b->mx - a->mx) * a->ux + (b->my - a->my) * a->uy;
            float t = 0.5f * (fmaxf(-a->half, tb - b->half) + fminf(a->half, tb + b->half));
            int mid = map_at(s, a->mx + a->ux * t + nx * q * 0.5f, a->my + a->uy * t + ny * q * 0.5f);
            if (!k && b_to_a && mid == 0) a->near_gap = (int16_t)j;
            if (k && !b_to_a && mid == 1) a->near_mat = (int16_t)j;
        }
    }
    /* mutual nearest partners -> one measurement of that pair */
    uint8_t *added = s->added;                                  /* bit k: pair (i, k) placed in phase 0 */
    memset(added, 0, n);
    for (int phase = 0; phase < 2; phase++)
    for (int i = 0; i < n; i++) {
        for (int k = 0; k < 2; k++) {
            if (added[i] & (1 << k)) continue;
            int j = k ? s->run[i].near_mat : s->run[i].near_gap;
            if (j <= i || (k ? s->run[j].near_mat : s->run[j].near_gap) != i) continue;
            const gc_run_t *a = &s->run[i], *b = &s->run[j];
            float ux = a->ux, uy = a->uy;
            if (ux < 0 || (ux == 0 && uy < 0)) { ux = -ux; uy = -uy; }        /* canonical direction */
            float nx = -uy, ny = ux;
            float oa = a->mx * nx + a->my * ny, ob = b->mx * nx + b->my * ny;
            float ta = a->mx * ux + a->my * uy, tb = b->mx * ux + b->my * uy;
            float t0 = fmaxf(ta - a->half, tb - b->half), t1 = fminf(ta + a->half, tb + b->half);
            float cs = fabsf(oa - ob);
            float sep = k ? cs + w : cs - w;                                 /* wall surfaces are w/2 off the bead centres */
            if (add_pair(s, k ? GC_WALL : GC_SLOT, ux, uy, 0.5f * (oa + ob), t0, t1, sep, h, phase)) added[i] |= (uint8_t)(1 << k);
        }
    }
    for (int i = s->n_ptrack - 1; i >= 0; i--) {
        if (!s->ptrack[i].seen) close_ptrack(s, i);
        else s->ptrack[i].seen = 0;
    }
}

/* ------------------------------------------------------------------ layers */

static void finish_layer(gc_state_t *s)
{
    end_path(s);
    float h = layer_h(s);
    float wc = median_circ_w(s);
    layer_circles(s, wc, h);
    if (GC_TILT) layer_ellipses(s, wc, h);
    if (GC_EDGES) layer_edges(s, median_edge_w(s), h);
    s->n_circ = 0;
    s->n_ell = 0;
    s->n_run = 0;
}

static void new_layer(gc_state_t *s, float z)
{
    end_path(s);
    if (s->in_layer && s->sampled) finish_layer(s);
    s->prev_z = s->in_layer ? s->layer_z : 0.0f;
    s->layer_z = z;
    s->in_layer = 1;
    s->layer_idx++;
    s->res.layers++;
    s->sampled = (s->layer_idx % (uint32_t)s->stride) == 0;
    if (s->sampled) {
        s->res.sampled++;
        memset(s->map, 0, (size_t)s->cap.map_n * s->cap.map_n / 8);
        s->map_init = 0;              /* re-centred on this layer's first extrusion (not the purge line) */
        s->n_circ = 0;
        s->n_ell = 0;
        s->n_run = 0;
        memset(s->whist, 0, sizeof s->whist);
    }
}

static void extrude(gc_state_t *s, float x0, float y0, float x1, float y1, float de)
{
    if (!s->in_layer || fabsf(s->z - s->layer_z) > 1e-4f) new_layer(s, s->z);
    float h = layer_h(s);
    if (s->z - h < s->zmin) s->zmin = s->z - h;
    if (s->z > s->zmax) s->zmax = s->z;
    if (x1 < s->xmin) s->xmin = x1;
    if (x1 > s->xmax) s->xmax = x1;
    if (y1 < s->ymin) s->ymin = y1;
    if (y1 > s->ymax) s->ymax = y1;
    if (!s->sampled) return;
    if (!s->map_init) {
        s->map_x0 = x0 - s->cap.map_n * GC_MAP_CELL / 2; s->map_y0 = y0 - s->cap.map_n * GC_MAP_CELL / 2; s->map_init = 1;
    }
    map_segment(s, x0, y0, x1, y1);
    path_segment(s, x0, y0, x1, y1, de);
}

/* ------------------------------------------------------------------ G-code lines */

const gc_caps_t gc_caps_full = {GC_PATH_MAX, GC_MAX_CIRC, GC_MAX_ELL, GC_MAX_RUN, GC_MAX_TRACK, GC_MAX_ETRACK,
                                GC_MAX_PTRACK, GC_MAX_ELEM, GC_MAP_N};
const gc_caps_t gc_caps_compact = {256, 96, 48, 160, 48, 16, 40, 128, 256};
const gc_caps_t gc_caps_tiny = {192, 64, 32, 128, 32, 12, 32, 96, 224};

#define ALIGN8(x) (((x) + 7u) & ~7u)

/* offsets of the arrays after the struct (s == NULL: only the size) */
static uint32_t layout(const gc_caps_t *c, gc_state_t *s, uint8_t *base)
{
    uint32_t off = ALIGN8((uint32_t)sizeof(gc_state_t));
#define PLACE(field, type, count)                                          \
    do {                                                                   \
        if (s) s->field = (type *)(void *)(base + off);                    \
        off = ALIGN8(off + (uint32_t)sizeof(type) * (uint32_t)(count));    \
    } while (0)
    PLACE(ppx, float, c->path);
    PLACE(ppy, float, c->path);
    PLACE(circ, gc_circ_t, c->circ);
    PLACE(ell, gc_ell_t, c->ell);
    PLACE(run, gc_run_t, c->run);
    PLACE(run_order, uint16_t, c->run);
    PLACE(map, uint8_t, (uint32_t)c->map_n * c->map_n / 8);
    PLACE(track, gc_track_t, c->track);
    PLACE(etrack, gc_etrack_t, c->etrack);
    PLACE(ptrack, gc_ptrack_t, c->ptrack);
    PLACE(elem, gc_elem_t, c->elem);
    PLACE(done, uint8_t, c->circ);
    PLACE(edone, uint8_t, c->ell);
    PLACE(fam, uint8_t, c->ell);
    PLACE(added, uint8_t, c->run);
#undef PLACE
    return off;
}

uint32_t gc_state_bytes(const gc_caps_t *caps)
{
    return layout(caps, 0, 0);
}

gc_state_t *gc_init(void *mem, uint32_t bytes, const gc_caps_t *caps, int stride)
{
    uint32_t need = gc_state_bytes(caps);
    if (!mem || ((uintptr_t)mem & 7u) || bytes < need || caps->map_n % 8 || !caps->path || !caps->run) return 0;
    gc_state_t *s = (gc_state_t *)mem;
    memset(mem, 0, need);
    s->cap = *caps;
    layout(caps, s, (uint8_t *)mem);
    s->abs_xyz = 1; s->abs_e = 1;
    s->stride = stride > 0 ? stride : 1;
    s->zmin = s->xmin = s->ymin = 1e9f; s->zmax = s->xmax = s->ymax = -1e9f;
    s->layer_idx = (uint32_t)-1;
    return s;
}

static void role_comment(gc_state_t *s, const char *p, const char *end)
{
    while (p < end && (*p == ';' || *p == ' ')) p++;
    if (!(starts_ci(p, end, "type:") || starts_ci(p, end, "feature:"))) return;
    s->has_roles = 1;
    s->excluded = contains_ci(p, end, "support") || contains_ci(p, end, "skirt") || contains_ci(p, end, "brim")
                  || contains_ci(p, end, "wipe") || contains_ci(p, end, "prime") || contains_ci(p, end, "purge");
    /* the outermost perimeter of every boundary, holes included: PrusaSlicer "External perimeter"
       (and "Overhang perimeter"), Orca / Bambu "Outer wall", Cura "WALL-OUTER" */
    s->outer = contains_ci(p, end, "external") || contains_ci(p, end, "outer") || contains_ci(p, end, "overhang");
}

void gc_line(gc_state_t *s, const char *line, int len)
{
    const char *p = line, *end = line + len, *semi = end;
    s->res.lines++;
    for (const char *q = line; q < end; q++) if (*q == ';') { semi = q; break; }
    if (semi < end) role_comment(s, semi, end);
    end = semi;
    while (p < end && (*p == ' ' || *p == '\t')) p++;
    if (p < end && (*p == 'N' || *p == 'n')) {                     /* line number */
        float dummy; const char *q; p++; parse_num(p, end, &dummy, &q); p = q;
        while (p < end && *p == ' ') p++;
    }
    if (p >= end) return;
    char cmd = *p;
    if (cmd != 'G' && cmd != 'M') return;
    float code; const char *q;
    if (!parse_num(p + 1, end, &code, &q)) return;
    p = q;
    int c = (int)code;
    if (cmd == 'M') {
        if (c == 82) s->abs_e = 1; else if (c == 83) s->abs_e = 0;
        return;
    }
    if (c == 90) { s->abs_xyz = 1; s->abs_e = 1; return; }
    if (c == 91) { s->abs_xyz = 0; s->abs_e = 0; return; }
    if (c != 0 && c != 1 && c != 2 && c != 3 && c != 92) return;
    float X = s->x, Y = s->y, Z = s->z, E = 0, I = 0, J = 0;
    int hx = 0, hy = 0, hz = 0, he = 0;
    while (p < end) {
        char k = *p;
        if (k >= 'a' && k <= 'z') k = (char)(k - 'a' + 'A');
        float v;
        if (k >= 'A' && k <= 'Z' && parse_num(p + 1, end, &v, &q)) {
            switch (k) {
            case 'X': X = v; hx = 1; break;
            case 'Y': Y = v; hy = 1; break;
            case 'Z': Z = v; hz = 1; break;
            case 'E': E = v; he = 1; break;
            case 'I': I = v; break;
            case 'J': J = v; break;
            default: break;
            }
            p = q;
        } else p++;
    }
    if (c == 92) {                       /* set position */
        if (he) s->e = E;
        if (hx) s->x = X;
        if (hy) s->y = Y;
        if (hz) s->z = Z;
        return;
    }
    if (!s->abs_xyz) { X = s->x + (hx ? X : 0); Y = s->y + (hy ? Y : 0); Z = s->z + (hz ? Z : 0); }
    float de = 0;
    if (he) { de = s->abs_e ? E - s->e : E; s->e = s->abs_e ? E : s->e + E; }
    float x0 = s->x, y0 = s->y;
    s->z = Z;
    int moved = fabsf(X - x0) > 1e-5f || fabsf(Y - y0) > 1e-5f;
    int extruding = de > 1e-6f && (moved || c >= 2) && !s->excluded && s->has_pos;
    if (!extruding) {
        if (moved) end_path(s);
    } else if (c <= 1) {
        extrude(s, x0, y0, X, Y, de);
    } else {                             /* G2 clockwise / G3 counter-clockwise arc, I J centre offset */
        float cx = x0 + I, cy = y0 + J;
        float r = sqrtf(I * I + J * J);
        float a0 = atan2f(y0 - cy, x0 - cx), a1 = atan2f(Y - cy, X - cx);
        float sweep = a1 - a0;
        if (c == 2) { if (sweep >= -1e-6f) sweep -= 2 * PI_F; }
        else if (sweep <= 1e-6f) sweep += 2 * PI_F;
        int n = (int)(fabsf(sweep) / ARC_STEP) + 1;
        float px = x0, py = y0;
        for (int k = 1; k <= n; k++) {
            float a = a0 + sweep * k / n;
            float nx = k == n ? X : cx + r * cosf(a), ny = k == n ? Y : cy + r * sinf(a);
            extrude(s, px, py, nx, ny, de / n);
            px = nx; py = ny;
        }
    }
    s->x = X; s->y = Y;
    if (hx || hy) s->has_pos = 1;
}

/* ------------------------------------------------------------------ decision */

static void add_evid(gc_state_t *s, uint8_t type, int ref, uint8_t fired, uint8_t n, float d, float len)
{
    s->res.block = 1;
    if (s->res.n_evid >= GC_MAX_EVID) return;
    gc_evid_t *e = &s->res.evid[s->res.n_evid++];
    e->type = type; e->ref = (int16_t)ref; e->fired = fired; e->n = n; e->d = d; e->len = len;
}

static int is_slab(uint8_t k) { return k == GC_WALL || k == GC_SLOT; }

/* fingerprint.hole_relation: (angle deg, separation mm), pose invariant */
static void relation(const gc_elem_t *e1, const gc_elem_t *e2, float *ang, float *sep)
{
    const float *a1 = e1->a, *a2 = e2->a;
    float dot = a1[0] * a2[0] + a1[1] * a2[1] + a1[2] * a2[2];
    float cosang = fminf(fabsf(dot), 1.0f);
    *ang = DEG(acosf(cosang));
    float d[3] = {e2->c[0] - e1->c[0], e2->c[1] - e1->c[1], e2->c[2] - e1->c[2]};
    float sg = dot >= 0 ? 1.0f : -1.0f, am[3] = {a1[0] + sg * a2[0], a1[1] + sg * a2[1], a1[2] + sg * a2[2]};
    float an = sqrtf(am[0] * am[0] + am[1] * am[1] + am[2] * am[2]);
    am[0] /= an; am[1] /= an; am[2] /= an;
    float dam = d[0] * am[0] + d[1] * am[1] + d[2] * am[2];
    int s1 = is_slab(e1->kind), s2 = is_slab(e2->kind);
    if (!s1 && !s2) {
        if (*ang < 1.0f) {
            float p0 = d[0] - dam * am[0], p1 = d[1] - dam * am[1], p2 = d[2] - dam * am[2];
            *sep = sqrtf(p0 * p0 + p1 * p1 + p2 * p2);
        } else {
            float n0 = a1[1] * a2[2] - a1[2] * a2[1], n1 = a1[2] * a2[0] - a1[0] * a2[2], n2 = a1[0] * a2[1] - a1[1] * a2[0];
            float nn = sqrtf(n0 * n0 + n1 * n1 + n2 * n2);
            *sep = fabsf(d[0] * n0 + d[1] * n1 + d[2] * n2) / nn;
        }
        return;
    }
    if (s1 && s2) { *sep = *ang < 1.0f ? fabsf(dam) : 0.0f; return; }
    const float *n = s1 ? a1 : a2;
    *sep = *ang > 89.0f ? fabsf(d[0] * n[0] + d[1] * n[1] + d[2] * n[2]) : 0.0f;
}

typedef struct { float bd, bsep, bang, blen; } band_t;

static int elem_ok(const fp_elem_t *fe, const gc_elem_t *e, const band_t *b)
{
    if (e->kind != fe->kind && !(fe->amb && fe->kind <= GC_BOSS && e->kind <= GC_BOSS)) return 0;
    if (fabsf(e->d - fe->d) > b->bd) return 0;
    if (fe->min_L > 0 && e->fill < fe->min_L - b->blen) return 0;    /* length with wall, not the merged span */
    return 1;
}

static int rel_idx(int i, int j, int n) { return i * (2 * n - i - 1) / 2 + (j - i - 1); }   /* i < j */

/* injective assignment of the fingerprint's elements onto the found elements (depth-first, <= 4 deep) */
static int assign(const gc_state_t *s, const fp_print_t *f, const band_t *b, int depth, int *as)
{
    const fp_elem_t *fe = &FP_ELEMS[f->e0];
    const fp_rel_t *fr = &FP_RELS[f->r0];
    if (depth == f->n) return 1;
    for (int k = 0; k < s->n_elem; k++) {
        const gc_elem_t *e = &s->elem[k];
        if (!elem_ok(&fe[depth], e, b)) continue;
        /* a horizontal cylinder's diameter comes from a chord fit, coarser than a circle's:
           at lineage level it does not make a two-element match (a buffer tower took an
           STI frame's label) */
        if (b->bd > BAND_TIGHT_D && f->n == 2 && e->kind <= GC_BOSS && fabsf(e->a[2]) < 0.99f) continue;
        int ok = 1;
        for (int i = 0; i < depth && ok; i++) {
            if (as[i] == k) { ok = 0; break; }
            float ang, sep;
            relation(&s->elem[as[i]], e, &ang, &sep);
            const fp_rel_t *r = &fr[rel_idx(i, depth, f->n)];
            if (fabsf(ang - r->ang) > b->bang || fabsf(sep - r->sep) > b->bsep) ok = 0;
        }
        if (!ok) continue;
        as[depth] = k;
        if (assign(s, f, b, depth + 1, as)) return 1;
    }
    return 0;
}

static int bore_caliber(float d, float L)
{
    for (int i = 0; i < BORE_N_METRIC; i++)
        if (L < BORE_LONG_L && fabsf(d - BORE_METRIC[i]) <= BORE_METRIC_TOL) return -1;
    for (int i = 0; i < BORE_N_RODS; i++)
        if (fabsf(d - BORE_RODS[i]) <= BORE_METRIC_TOL || (d >= BORE_RODS[i] && d <= BORE_RODS[i] + BORE_METRIC_CLEARANCE))
            return -1;
    for (int i = 0; i < BORE_N_BULLETS; i++)
        if (d >= BORE_BULLETS[i] - BORE_TOL_UNDER && d <= BORE_BULLETS[i] + BORE_TOL_OVER) return i;
    return -1;
}

static float axis_dist(const gc_elem_t *a, const gc_elem_t *b)
{
    float d[3] = {b->c[0] - a->c[0], b->c[1] - a->c[1], b->c[2] - a->c[2]};
    float t = d[0] * a->a[0] + d[1] * a->a[1] + d[2] * a->a[2];
    float p0 = d[0] - t * a->a[0], p1 = d[1] - t * a->a[1], p2 = d[2] - t * a->a[2];
    return sqrtf(p0 * p0 + p1 * p1 + p2 * p2);
}

static int parallel(const gc_elem_t *a, const gc_elem_t *b, float cosmin)
{
    return fabsf(a->a[0] * b->a[0] + a->a[1] * b->a[1] + a->a[2] * b->a[2]) >= cosmin;
}

void gc_finish(gc_state_t *s)
{
    end_path(s);
    if (s->in_layer && s->sampled) finish_layer(s);
    while (s->n_track) close_track(s, s->n_track - 1);
    while (s->n_etrack) close_etrack(s, s->n_etrack - 1);
    while (s->n_ptrack) close_ptrack(s, s->n_ptrack - 1);
    /* duplicates (a pin bore through two walls; the same face pair found twice): one element */
    for (int i = 0; i < s->n_elem; i++)
        for (int j = s->n_elem - 1; j > i; j--) {
            gc_elem_t *a = &s->elem[i], *b = &s->elem[j];
            if (a->kind != b->kind || fabsf(a->d - b->d) > BAND_TIGHT_D || !parallel(a, b, 0.99996f)) continue;
            float off = is_slab(a->kind)
                ? fabsf((b->c[0] - a->c[0]) * a->a[0] + (b->c[1] - a->c[1]) * a->a[1] + (b->c[2] - a->c[2]) * a->a[2])
                : axis_dist(a, b);
            if (off > BAND_TIGHT_SEP) continue;
            if (is_slab(a->kind)) {
                if (b->L > a->L) *a = *b;
            } else {
                /* coaxial pieces (a pin bore through two walls, a bore between chambers): one
                   cylinder over their union, as the full checker fits it; fill = length with wall */
                float ta = a->c[0] * a->a[0] + a->c[1] * a->a[1] + a->c[2] * a->a[2];
                float tb = b->c[0] * a->a[0] + b->c[1] * a->a[1] + b->c[2] * a->a[2];
                float lo = fminf(ta - a->L / 2, tb - b->L / 2), hi = fmaxf(ta + a->L / 2, tb + b->L / 2);
                float ov = fminf(ta + a->L / 2, tb + b->L / 2) - fmaxf(ta - a->L / 2, tb - b->L / 2);
                float fill = a->fill + b->fill - fmaxf(ov, 0.0f);
                float dm = 0.5f * (lo + hi) - ta;
                for (int k = 0; k < 3; k++) a->c[k] += dm * a->a[k];
                a->L = hi - lo;
                a->fill = fminf(fill, a->L);
            }
            s->elem[j] = s->elem[--s->n_elem];
        }
    s->res.elements = s->n_elem;
    uint8_t *fired = s->fired, *tight = s->tight;
    memset(tight, 0, FP_N_PARTS);
    for (int bnd = 0; bnd < 2; bnd++) {
        band_t b = bnd ? (band_t){BAND_LOOSE_D, BAND_LOOSE_SEP, BAND_LOOSE_ANG, BAND_LOOSE_LEN}
                       : (band_t){BAND_TIGHT_D, BAND_TIGHT_SEP, BAND_TIGHT_ANG, BAND_TIGHT_LEN};
        memset(fired, 0, FP_N_PARTS);
        for (int k = 0; k < FP_N_PRINTS; k++) {
            if (bnd && (FP_PRINTS[k].flags & GC_LOOSE_SKIP)) continue;   /* weak shapes: design level only */
            if (FP_PRINTS[k].flags & GC_SKIP_ALWAYS) continue;
            int as[4];
            if (assign(s, &FP_PRINTS[k], &b, 0, as)) fired[FP_PRINTS[k].part]++;
        }
        for (int p = 0; p < FP_N_PARTS; p++) {
            const fp_part_t *P = &FP_PARTS[p];
            if (fired[p] < P->min_fired || (bnd && tight[p])) continue;
            if (P->grip && (bnd || fired[p] < P->n_variants)) continue;   /* grips: exact copies only */
            if (!bnd) tight[p] = 1;
            add_evid(s, bnd ? GC_EV_LINEAGE : GC_EV_DESIGN, p, fired[p], P->n_variants, 0, 0);
        }
    }
    /* barrel bore: a long hole at a bullet diameter that, with its coaxial holes (chamber),
       covers the part's whole extent along its axis (vertical: height; horizontal: the
       print's footprint along the axis, an upper bound on the part's length) */
    for (int i = 0; i < s->n_elem; i++) {
        const gc_elem_t *c = &s->elem[i];
        if (c->kind != GC_HOLE || c->L < BORE_MIN_L || c->L < BORE_MIN_LD * c->d) continue;
        if (c->fill < BORE_MIN_FILL * c->L) continue;          /* calibers.classify_diameter: a real surface */
        int cal = bore_caliber(c->d, c->L);
        if (cal < 0) continue;
        /* extent of the print's bounding box along the axis: an upper bound on the part's length */
        float elo = 1e9f, ehi = -1e9f;
        for (int k = 0; k < 8; k++) {
            float t3 = (k & 1 ? s->xmax : s->xmin) * c->a[0] + (k & 2 ? s->ymax : s->ymin) * c->a[1]
                     + (k & 4 ? s->zmax : s->zmin) * c->a[2];
            if (t3 < elo) elo = t3;
            if (t3 > ehi) ehi = t3;
        }
        float extent = ehi - elo;
        if (extent <= 0) continue;
        float iv[16][2];
        int n = 0;
        for (int j = 0; j < s->n_elem && n < 16; j++) {
            const gc_elem_t *o = &s->elem[j];
            if (o->kind != GC_HOLE || !parallel(c, o, 0.99939f) || axis_dist(c, o) > COAX_TOL) continue;
            float t = o->c[0] * c->a[0] + o->c[1] * c->a[1] + o->c[2] * c->a[2];
            int k = n++;
            while (k > 0 && iv[k - 1][0] > t - o->L / 2) { iv[k][0] = iv[k - 1][0]; iv[k][1] = iv[k - 1][1]; k--; }
            iv[k][0] = t - o->L / 2; iv[k][1] = t + o->L / 2;
        }
        float span = 0, lo = iv[0][0], hi = iv[0][1];
        for (int k = 1; k < n; k++) {
            if (iv[k][0] > hi) { span += hi - lo; lo = iv[k][0]; hi = iv[k][1]; }
            else if (iv[k][1] > hi) hi = iv[k][1];
        }
        span += hi - lo;
        if (span / extent >= BORE_MIN_SPAN) add_evid(s, GC_EV_BORE, cal, 0, 0, c->d, c->L);
    }
#ifdef GC_DEBUG
    for (int i = 0; i < s->n_elem; i++) {
        const gc_elem_t *e = &s->elem[i];
        fprintf(stderr, "elem %d a (%.3f %.3f %.3f) c (%.2f %.2f %.2f) d %.3f L %.2f fill %.2f\n", e->kind, e->a[0], e->a[1], e->a[2],
                e->c[0], e->c[1], e->c[2], e->d, e->L, e->fill);
    }
#endif
}

const char *gc_part_name(int ref) { return ref >= 0 && ref < FP_N_PARTS ? FP_PARTS[ref].name : "?"; }
const char *gc_bore_name(int ref) { return ref >= 0 && ref < BORE_N_BULLETS ? BORE_NAMES[ref] : "?"; }
