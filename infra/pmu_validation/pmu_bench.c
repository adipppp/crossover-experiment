/*
 * pmu_bench.c — Micro-benchmark untuk validasi fidelitas hardware performance
 * counter (PMU) pada VM cloud sebelum eksperimen utama dijalankan.
 *
 * Menghasilkan dua pola akses memori yang kontras secara sengaja:
 *   MODE "high" : akses acak ke array 64 MB (jauh di atas kapasitas LLC)
 *                 → cache-miss rate TINGGI (ekspektasi: >90%)
 *   MODE "low"  : akses sekuensial ke array 16 KB (muat di L1 cache)
 *                 → cache-miss rate RENDAH (ekspektasi: <1% setelah warm-up)
 *
 * Jika perf stat melaporkan nilai yang tidak berbeda bermakna antara dua mode ini,
 * hardware performance counter dianggap TIDAK valid pada VM ini dan metrik PMU
 * tidak digunakan dalam analisis utama (lihat Subbab "Validasi Fidelitas PMU").
 *
 * Penggunaan (dipanggil oleh validate_pmu_fidelity.py, BUKAN langsung):
 *   ./pmu_bench high [ITERASI]
 *   ./pmu_bench low  [ITERASI]
 *
 * Kompilasi:
 *   gcc -O2 -m64 -o pmu_bench pmu_bench.c
 *
 * CATATAN TEKNIS:
 *   - Variabel sink bersifat volatile untuk mencegah dead-code elimination.
 *   - Array high-miss dialokasikan via mmap (bukan stack) agar ukuran 64 MB
 *     tidak menyebabkan stack overflow.
 *   - LCG (linear congruential generator) dipakai sebagai sumber indeks acak
 *     yang deterministik dan murah (tidak ada overhead syscall rand()).
 *   - Ukuran array dipilih berdasarkan L3 cache c2-standard-8 (~16.5 MB);
 *     64 MB memastikan miss rate tinggi bahkan pada varian Intel dengan L3 besar.
 *   - Iterasi default 10 juta (high) / 200 juta (low) menghasilkan durasi
 *     eksekusi ~0.5–2 detik, cukup untuk pembacaan perf stat yang stabil.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <sys/mman.h>

/* ── Ukuran array ──────────────────────────────────────────────────────────── */

/* Array high-miss: 64 MB = 16 juta elemen uint32_t (@ 4 bytes/elemen)       */
#define HIGH_ARRAY_BYTES   (64UL * 1024 * 1024)
#define HIGH_ARRAY_ELEMS   (HIGH_ARRAY_BYTES / sizeof(uint32_t))

/* Array low-miss: 16 KB = 4096 elemen uint32_t                               */
#define LOW_ARRAY_BYTES    (16UL * 1024)
#define LOW_ARRAY_ELEMS    (LOW_ARRAY_BYTES / sizeof(uint32_t))

/* ── Iterasi default ───────────────────────────────────────────────────────── */
#define DEFAULT_ITER_HIGH  10000000UL   /* 10 juta akses acak                 */
#define DEFAULT_ITER_LOW   200000000UL  /* 200 juta akses sekuensial          */

/* ── LCG (Knuth Vol.2) ─────────────────────────────────────────────────────── */
static inline uint64_t lcg_next(uint64_t state) {
    return state * 6364136223846793005ULL + 1442695040888963407ULL;
}

/* ── Sink global (volatile mencegah dead-code elimination) ─────────────────── */
volatile uint64_t global_sink = 0;

/* ─────────────────────────────────────────────────────────────────────────── */
/* MODE HIGH: akses acak ke 64 MB array                                        */
/* ─────────────────────────────────────────────────────────────────────────── */

static int run_high(unsigned long iters) {
    uint32_t *arr = (uint32_t *)mmap(
        NULL, HIGH_ARRAY_BYTES,
        PROT_READ | PROT_WRITE,
        MAP_PRIVATE | MAP_ANONYMOUS, -1, 0
    );
    if (arr == MAP_FAILED) {
        perror("mmap gagal (mode high)");
        return 1;
    }

    /* Inisialisasi array dengan nilai yang bukan nol agar page fault terjadi
     * sekarang (bukan saat akses pertama di loop pengukuran). */
    memset(arr, 0xAB, HIGH_ARRAY_BYTES);

    uint64_t state = 0xDEADBEEFCAFEBABEULL;
    uint64_t sink  = 0;

    for (unsigned long i = 0; i < iters; i++) {
        state = lcg_next(state);
        uint64_t idx = state % HIGH_ARRAY_ELEMS;
        sink += arr[idx];
    }

    global_sink += sink;
    munmap(arr, HIGH_ARRAY_BYTES);
    return 0;
}

/* ─────────────────────────────────────────────────────────────────────────── */
/* MODE LOW: akses sekuensial ke 16 KB array (muat di L1 cache)               */
/* ─────────────────────────────────────────────────────────────────────────── */

static int run_low(unsigned long iters) {
    uint32_t arr[LOW_ARRAY_ELEMS];

    /* Inisialisasi */
    for (size_t i = 0; i < LOW_ARRAY_ELEMS; i++)
        arr[i] = (uint32_t)i;

    /* Satu pass pertama untuk memanaskan cache (warm-up); akses pengukuran
     * dimulai dari sini sehingga warm-up termasuk dalam window perf stat.
     * Warm-up yang cepat (~4096 akses) tidak mempengaruhi miss rate agregat
     * secara signifikan pada 200 juta iterasi total. */

    uint64_t sink = 0;
    for (unsigned long i = 0; i < iters; i++) {
        sink += arr[i % LOW_ARRAY_ELEMS];
    }

    global_sink += sink;
    return 0;
}

/* ─────────────────────────────────────────────────────────────────────────── */
/* main                                                                         */
/* ─────────────────────────────────────────────────────────────────────────── */

int main(int argc, char *argv[]) {
    if (argc < 2) {
        fprintf(stderr, "Penggunaan: %s <high|low> [ITERASI]\n", argv[0]);
        fprintf(stderr, "  high : akses acak ke 64 MB array (cache-miss rate TINGGI)\n");
        fprintf(stderr, "  low  : akses sekuensial ke 16 KB array (cache-miss rate RENDAH)\n");
        return 2;
    }

    const char *mode = argv[1];
    unsigned long iters = 0;

    if (argc >= 3) {
        iters = strtoul(argv[2], NULL, 10);
        if (iters == 0) {
            fprintf(stderr, "ERROR: ITERASI harus bilangan bulat positif.\n");
            return 2;
        }
    }

    if (strcmp(mode, "high") == 0) {
        if (iters == 0) iters = DEFAULT_ITER_HIGH;
        return run_high(iters);
    } else if (strcmp(mode, "low") == 0) {
        if (iters == 0) iters = DEFAULT_ITER_LOW;
        return run_low(iters);
    } else {
        fprintf(stderr, "ERROR: Mode tidak dikenal: '%s'. Gunakan 'high' atau 'low'.\n", mode);
        return 2;
    }
}
