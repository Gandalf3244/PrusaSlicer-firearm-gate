/* Host driver: G-code text on stdin -> one JSON verdict line on stdout.
 *     gcheck_host [stride] [full|compact|tiny]        gcheck_host --size
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "gcheck.h"

int main(int argc, char **argv)
{
    if (argc > 1 && !strcmp(argv[1], "--size")) {
        printf("{\"state_bytes\": %u, \"compact\": %u, \"tiny\": %u}\n", (unsigned)gc_state_bytes(&gc_caps_full),
               (unsigned)gc_state_bytes(&gc_caps_compact), (unsigned)gc_state_bytes(&gc_caps_tiny));
        return 0;
    }
    int stride = argc > 1 ? atoi(argv[1]) : 1;
    const gc_caps_t *caps = &gc_caps_full;
    if (argc > 2 && !strcmp(argv[2], "compact")) caps = &gc_caps_compact;
    if (argc > 2 && !strcmp(argv[2], "tiny")) caps = &gc_caps_tiny;
    uint32_t bytes = gc_state_bytes(caps);
    void *mem = malloc(bytes);
    gc_state_t *S = gc_init(mem, bytes, caps, stride);
    if (!S) {
        fprintf(stderr, "gc_init failed\n");
        return 2;
    }
    static char buf[1 << 16];
    while (fgets(buf, sizeof buf, stdin)) {
        int n = (int)strlen(buf);
        while (n && (buf[n - 1] == '\n' || buf[n - 1] == '\r')) n--;
        gc_line(S, buf, n);
    }
    gc_finish(S);
    const gc_result_t *r = &S->res;
    printf("{\"block\": %d, \"evidence\": [", r->block);
    for (int i = 0; i < r->n_evid; i++) {
        const gc_evid_t *e = &r->evid[i];
        if (e->type == GC_EV_BORE)
            printf("%s\"bore: %s (d %.2f x %.0f mm)\"", i ? ", " : "", gc_bore_name(e->ref), e->d, e->len);
        else
            printf("%s\"%s: %s %d/%d\"", i ? ", " : "", e->type == GC_EV_DESIGN ? "design" : "lineage",
                   gc_part_name(e->ref), e->fired, e->n);
    }
    printf("], \"lines\": %u, \"layers\": %u, \"sampled\": %u, \"loops\": %u, \"circles\": %u, \"cylinders\": %u, "
           "\"v_cyl\": %u, \"h_cyl\": %u, \"t_cyl\": %u, \"ellipses\": %u, \"slabs\": %u, \"t_slabs\": %u, \"pairs\": %u, "
           "\"max_circ\": %u, \"max_track\": %u, \"max_run\": %u, \"max_ptrack\": %u, \"max_ell\": %u, \"max_etrack\": %u, "
           "\"overflow\": %u}\n",
           r->lines, r->layers, r->sampled, r->loops, r->circles, r->elements, r->v_cyl, r->h_cyl, r->t_cyl, r->ellipses, r->slabs, r->t_slabs, r->pairs,
           r->max_circ, r->max_track, r->max_run, r->max_ptrack, r->max_ell, r->max_etrack, r->overflow);
    free(mem);
    return 0;
}
