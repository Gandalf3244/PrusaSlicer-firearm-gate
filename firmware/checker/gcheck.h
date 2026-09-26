/* gcheck - streaming firearm-part check of sliced G-code for small microcontrollers.
 *
 * Feed the G-code one line at a time (gc_line), then call gc_finish. All state
 * lives in one fixed-size gc_state_t owned by the caller: no dynamic memory,
 * no file access, recursion only as deep as a fingerprint's element count (4).
 *
 * What it sees, from the wall loops of every `stride`-th layer:
 *   - holes / bosses whose axis is vertical in the print: circles repeated
 *     layer after layer;
 *   - holes / bosses whose axis is tilted: ellipses whose centre moves along
 *     the major axis from layer to layer, as far as the ellipse's shape says;
 *   - holes / bosses lying horizontally: two straight wall edges facing each
 *     other (or back to back) whose separation follows a circle's chord from
 *     layer to layer;
 *   - slots and walls: such edge pairs at a constant separation, vertical
 *     (a fixed midline) or tilted (a midline that moves with the height);
 *   - the full-length barrel bore rule, for vertical and horizontal bores.
 * These are matched against the fingerprints of fp_table.h
 * (scripts/mcu_table.py) with the full checker's rules (TIGHT = design,
 * LOOSE = lineage, k-of-n, the corroboration tier, grips exact-only, weak
 * shapes at design level only). Comments are optional; when the slicer writes
 * feature types, support, skirt, brim and wipe tower are ignored.
 */
#ifndef GCHECK_H
#define GCHECK_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#ifndef GC_LOOSE_SKIP
#define GC_LOOSE_SKIP 7          /* flags matching at design level only: M3-size pairs, stepped tubes, equal pairs */
#endif
#ifndef GC_SKIP_ALWAYS
#define GC_SKIP_ALWAYS 4         /* flags never matched: equal pairs (a Charon lower matched a Revenant frame) */
#endif
#ifndef GC_EDGES
#define GC_EDGES 1               /* 0: vertical cylinders only (the first version) */
#endif
#ifndef GC_TILT
#define GC_TILT 1                /* 0: no tilted cylinders (ellipse loops) */
#endif

/* capacities (RAM; each can be set at compile time, e.g. -DGC_MAX_RUN=256) */
#ifndef GC_PATH_MAX
#define GC_PATH_MAX 512          /* points kept of a loop that may be a circle (2 x 4 bytes each) */
#endif
#ifndef GC_MAX_CIRC
#define GC_MAX_CIRC 128          /* circular closed loops kept per sampled layer */
#endif
#ifndef GC_MAX_RUN
#define GC_MAX_RUN 256           /* straight wall edges kept per sampled layer */
#endif
#ifndef GC_MAX_ELL
#define GC_MAX_ELL 64            /* elliptical closed loops kept per sampled layer */
#endif
#ifndef GC_MAX_ETRACK
#define GC_MAX_ETRACK 32         /* tilted cylinders being followed across layers */
#endif
#ifndef GC_MAX_TRACK
#define GC_MAX_TRACK 96          /* vertical cylinders being followed across layers */
#endif
#ifndef GC_MAX_PTRACK
#define GC_MAX_PTRACK 64         /* edge pairs being followed across layers */
#endif
#ifndef GC_MAX_ELEM
#define GC_MAX_ELEM 192          /* finished elements (cylinders and slabs) */
#endif
#define GC_NBIN 90               /* edge direction bins (2 deg) */
#define GC_WHIST 190             /* bead-width histogram bins (0.10 - 2.00 mm) */
#define GC_MAX_EVID 8            /* evidence lines reported */
#define GC_MAX_PARTS 512         /* parts in fp_table.h (per-part counters while matching) */
#ifndef GC_MAP_N
#define GC_MAP_N 320             /* occupancy bitmap: GC_MAP_N x GC_MAP_N cells (a multiple of 8) ... */
#endif
#define GC_MAP_CELL 1.0f         /* ... of this size (mm), centred on each layer's first extrusion */

enum { GC_HOLE = 0, GC_BOSS = 1, GC_WALL = 2, GC_SLOT = 3 };

typedef struct { const char *name; uint8_t n_variants, min_fired, grip; } fp_part_t;
typedef struct { uint16_t part; uint8_t n, flags; uint16_t e0, r0; } fp_print_t;   /* flags: see mcu_table.py */
typedef struct { float d, min_L; uint8_t kind, amb; } fp_elem_t;
typedef struct { float ang, sep; } fp_rel_t;                                        /* pairs 0-1, 0-2, .., in order */

typedef struct { float cx, cy, rc, w; } gc_circ_t;
typedef struct { float mx, my, ux, uy, half, off; int8_t side; uint8_t fam, bin; int16_t near_gap, near_mat; } gc_run_t;
typedef struct { float cx, cy, sum_r, z0, z1; uint16_t n; uint8_t kind, seen; } gc_track_t;
typedef struct { float cx, cy, a, b, ux, uy, w; } gc_ell_t;          /* semi-axes a >= b, (ux, uy) along a */
typedef struct {
    float x0, y0, zref, z0, z1, lx, ly, lz, vx, vy, sum_b, sum_ratio, c2, s2;
    double sz, szz, sx, szx, sy, szy, sxx, syy;     /* centre against height, relative to the first layer */
    uint16_t n;
    uint8_t kind, seen;
} gc_etrack_t;
typedef struct {
    float ux, uy, m, t0, t1, z0, z1, zref, s_min, s_max, lm, lz, ls, k;   /* l*: last layer; k = dm / dz */
    double s_sum, s_sq, fz, fzz, fy, fzy, fyy, fm, fzm, fmm;   /* a few samples per layer: double is cheap here */
    uint16_t n;
    uint8_t kind, seen;                            /* kind: GC_SLOT = gap between the edges, GC_WALL = material */
    uint8_t drift;                                 /* joined as a tilted face: its midline moves by k per mm */
} gc_ptrack_t;
typedef struct { float a[3], c[3], d, L, fill; uint8_t kind; } gc_elem_t;   /* fill: length with wall (coaxial pieces merged) */

enum { GC_EV_DESIGN = 0, GC_EV_LINEAGE = 1, GC_EV_BORE = 2 };
typedef struct { uint8_t type, fired, n; int16_t ref; float d, len; } gc_evid_t;

typedef struct {
    int block;                    /* 1 = do not print */
    int n_evid;
    gc_evid_t evid[GC_MAX_EVID];
    /* statistics */
    uint32_t lines, layers, sampled, loops, circles, elements;
    uint32_t v_cyl, h_cyl, slabs, t_slabs, pairs, ellipses, t_cyl;
    uint16_t max_circ, max_track, max_run, max_ptrack, max_ell, max_etrack;
    uint8_t overflow;             /* bit 0 circles, 1 tracks, 2 elements, 3 runs, 4 pair tracks, 5 ellipses,
                                     6 tilted tracks dropped (never a crash) */
} gc_result_t;

/* Capacities of the per-file state. The state lives in one block given by the caller, laid
   out by gc_init; smaller capacities need less memory and lose a little recall (overflowing
   drops data, never writes out of bounds). */
typedef struct { uint16_t path, circ, ell, run, track, etrack, ptrack, elem, map_n; } gc_caps_t;
extern const gc_caps_t gc_caps_full;      /* the GC_* defaults above (~55 KB) */
extern const gc_caps_t gc_caps_compact;   /* ~35 KB */
extern const gc_caps_t gc_caps_tiny;      /* ~27 KB */

typedef struct {
    gc_caps_t cap;
    /* parser */
    float x, y, z, e;
    uint8_t abs_xyz, abs_e, excluded, has_pos, has_roles, outer;   /* outer: labelled external perimeter */
    int stride;
    /* extents of the model */
    float layer_z, prev_z, zmin, zmax, xmin, xmax, ymin, ymax;
    uint32_t layer_idx;
    uint8_t in_layer, sampled;
    /* current path (extrusion polyline) */
    uint8_t in_path;
    float px0, py0, plen, pe, lastx, lasty;
    uint32_t pn;
    uint8_t p_nocirc;             /* too many points or too wide to be a circle: not kept */
    float bx0, bx1, by0, by1;
    float *ppx, *ppy;             /* [cap.path] */
    /* current straight run of the path */
    uint8_t run_on;
    float rx0, ry0, rx1, ry1, rux, ruy;
    uint16_t path_run0;
    /* per sampled layer */
    gc_circ_t *circ;              /* [cap.circ] */
    uint16_t n_circ;
    gc_ell_t *ell;                /* [cap.ell] */
    uint16_t n_ell;
    uint16_t whist[GC_WHIST];     /* closed-loop bead widths of the layer, 0.01 mm bins from 0.10 mm */
    gc_run_t *run;                /* [cap.run] */
    uint16_t *run_order;          /* [cap.run] sorted by direction bin, then offset across the direction */
    uint16_t bin_start[GC_NBIN + 1];
    uint16_t n_run;
    uint8_t *map;                 /* [cap.map_n * cap.map_n / 8] */
    float map_x0, map_y0;
    uint8_t map_init;
    /* across layers */
    gc_track_t *track;            /* [cap.track] */
    uint16_t n_track;
    gc_etrack_t *etrack;          /* [cap.etrack] */
    uint16_t n_etrack;
    gc_ptrack_t *ptrack;          /* [cap.ptrack] */
    uint16_t n_ptrack;
    gc_elem_t *elem;              /* [cap.elem] */
    uint16_t n_elem;
    /* scratch (here, not on the caller's stack: the printer runs this on a 4 KB thread stack) */
    uint8_t *done, *edone, *fam, *added;   /* [cap.circ], [cap.ell], [cap.ell], [cap.run] */
    uint16_t cnt[GC_NBIN];
    uint8_t fired[GC_MAX_PARTS], tight[GC_MAX_PARTS];
    gc_result_t res;
} gc_state_t;

/* Bytes of the block gc_init needs for these capacities. */
uint32_t gc_state_bytes(const gc_caps_t *caps);
/* Lays the state out in mem (8-byte aligned, >= gc_state_bytes(caps) bytes) and resets it;
   NULL if the block is too small or misaligned. Every `stride`-th layer is looked at. */
gc_state_t *gc_init(void *mem, uint32_t bytes, const gc_caps_t *caps, int stride);
void gc_line(gc_state_t *s, const char *line, int len);
void gc_finish(gc_state_t *s);
const char *gc_part_name(int ref);
const char *gc_bore_name(int ref);

#ifdef __cplusplus
}
#endif

#endif
