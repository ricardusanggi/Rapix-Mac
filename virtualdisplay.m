// virtualdisplay.m — membuat monitor virtual di macOS agar tablet bisa jadi layar KEDUA (extend).
// Memakai API CGVirtualDisplay bawaan macOS (sama seperti yang dipakai BetterDisplay & Chromium).
// Layar virtual hidup selama program ini berjalan; begitu ditutup, layarnya hilang.
//
// Pemakaian: ./virtualdisplay LEBAR TINGGI [hidpi 0/1] [refresh Hz, bawaan 60]

#import <Foundation/Foundation.h>
#import <CoreGraphics/CoreGraphics.h>

@interface CGVirtualDisplayDescriptor : NSObject
@property (retain, nonatomic) dispatch_queue_t queue;
@property (retain, nonatomic) NSString *name;
@property (nonatomic) unsigned int maxPixelsHigh;
@property (nonatomic) unsigned int maxPixelsWide;
@property (nonatomic) CGSize sizeInMillimeters;
@property (nonatomic) unsigned int productID;
@property (nonatomic) unsigned int vendorID;
@property (nonatomic) unsigned int serialNum;
@property (copy, nonatomic) void (^terminationHandler)(id, id);
@end

@interface CGVirtualDisplayMode : NSObject
- (instancetype)initWithWidth:(unsigned int)width height:(unsigned int)height refreshRate:(double)refreshRate;
@end

@interface CGVirtualDisplaySettings : NSObject
@property (nonatomic) unsigned int hiDPI;
@property (retain, nonatomic) NSArray *modes;
@end

@interface CGVirtualDisplay : NSObject
@property (readonly, nonatomic) unsigned int displayID;
- (instancetype)initWithDescriptor:(CGVirtualDisplayDescriptor *)descriptor;
- (BOOL)applySettings:(CGVirtualDisplaySettings *)settings;
@end

static CGVirtualDisplay *gDisplay; // disimpan global supaya tidak dibebaskan

// pilih mode w x h dengan kepadatan piksel yang diminta (scale 2 = Retina) bila belum aktif;
// refresh rate diatur Rapix sendiri (kabel/Wi-Fi), jadi di sini hanya dipakai untuk pilihan pertama
static void forceMode(CGDirectDisplayID did, unsigned int w, unsigned int h, unsigned int scale, double hz) {
    CGDisplayModeRef cur = CGDisplayCopyDisplayMode(did);
    if (!cur) return;
    BOOL already = CGDisplayModeGetWidth(cur) == w && CGDisplayModeGetPixelWidth(cur) == w * scale;
    CGDisplayModeRelease(cur);
    if (already) return;

    NSDictionary *opts = @{(__bridge NSString *)kCGDisplayShowDuplicateLowResolutionModes: @YES};
    CFArrayRef modes = CGDisplayCopyAllDisplayModes(did, (__bridge CFDictionaryRef)opts);
    CGDisplayModeRef best = NULL;
    for (CFIndex i = 0; modes && i < CFArrayGetCount(modes); i++) {
        CGDisplayModeRef m = (CGDisplayModeRef)CFArrayGetValueAtIndex(modes, i);
        if (CGDisplayModeGetWidth(m) != w || CGDisplayModeGetHeight(m) != h || CGDisplayModeGetPixelWidth(m) != w * scale)
            continue;
        if (!best || fabs(CGDisplayModeGetRefreshRate(m) - hz) < 1) best = m;
    }
    if (best) {
        CGDisplayConfigRef cfg;
        CGBeginDisplayConfiguration(&cfg);
        CGConfigureDisplayWithDisplayMode(cfg, did, best, NULL);
        CGCompleteDisplayConfiguration(cfg, kCGConfigurePermanently);
    }
    if (modes) CFRelease(modes);
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc < 3) {
            fprintf(stderr, "pemakaian: virtualdisplay LEBAR TINGGI [hidpi]\n");
            return 1;
        }
        unsigned int w = (unsigned int)atoi(argv[1]);
        unsigned int h = (unsigned int)atoi(argv[2]);
        BOOL hidpi = argc > 3 && atoi(argv[3]) != 0;
        double hz = argc > 4 ? atof(argv[4]) : 60;
        unsigned int scale = hidpi ? 2 : 1;

        Class Desc = NSClassFromString(@"CGVirtualDisplayDescriptor");
        Class Disp = NSClassFromString(@"CGVirtualDisplay");
        Class Sett = NSClassFromString(@"CGVirtualDisplaySettings");
        Class Mode = NSClassFromString(@"CGVirtualDisplayMode");
        if (!Desc || !Disp || !Sett || !Mode) {
            fprintf(stderr, "macOS ini tidak mendukung layar virtual (butuh macOS 11 atau lebih baru)\n");
            return 2;
        }

        CGVirtualDisplayDescriptor *desc = [[Desc alloc] init];
        desc.queue = dispatch_get_main_queue();
        desc.name = @"Rapix";
        desc.maxPixelsWide = w * scale;
        desc.maxPixelsHigh = h * scale;
        double ppi = hidpi ? 220.0 : 110.0;
        desc.sizeInMillimeters = CGSizeMake(25.4 * w * scale / ppi, 25.4 * h * scale / ppi);
        desc.vendorID = 0x5250;   // "RP"
        desc.productID = 0x0002;
        desc.serialNum = 0x0001;
        desc.terminationHandler = ^(id a, id b) { exit(0); };

        gDisplay = [[Disp alloc] initWithDescriptor:desc];
        if (!gDisplay) {
            fprintf(stderr, "gagal membuat layar virtual\n");
            return 3;
        }

        CGVirtualDisplaySettings *settings = [[Sett alloc] init];
        settings.hiDPI = hidpi ? 1 : 0;
        // mode refresh tinggi + 60 Hz: Rapix memilih 60 Hz saat tablet lewat Wi-Fi (hemat), tinggi saat kabel
        settings.modes = hz != 60
            ? @[ [[Mode alloc] initWithWidth:w height:h refreshRate:hz], [[Mode alloc] initWithWidth:w height:h refreshRate:60] ]
            : @[ [[Mode alloc] initWithWidth:w height:h refreshRate:60] ];
        BOOL ok = [gDisplay applySettings:settings];
        if (!ok && hz != 60) {  // refresh tinggi ditolak: kembali ke 60 Hz
            fprintf(stderr, "refresh %.0f Hz ditolak, memakai 60 Hz\n", hz);
            settings.modes = @[ [[Mode alloc] initWithWidth:w height:h refreshRate:60] ];
            ok = [gDisplay applySettings:settings];
        }
        if (!ok) {
            fprintf(stderr, "gagal menerapkan resolusi %ux%u\n", w, h);
            return 4;
        }

        printf("READY %u\n", gDisplay.displayID);
        fflush(stdout);

        // macOS mengingat mode terakhir layar ini (mis. 1600x1052 tanpa Retina) dan bisa memakainya lagi,
        // sehingga teks di tablet buram. Paksa mode yang diminta setelah layar benar-benar terpasang.
        CGDirectDisplayID did = gDisplay.displayID;
        for (int i = 1; i <= 3; i++) {
            dispatch_after(dispatch_time(DISPATCH_TIME_NOW, i * NSEC_PER_SEC), dispatch_get_main_queue(), ^{
                forceMode(did, w, h, scale, hz);
            });
        }

        // Tutup otomatis saat program induk (server.py) berhenti.
        dispatch_async(dispatch_get_global_queue(QOS_CLASS_UTILITY, 0), ^{
            char buf[64];
            while (fgets(buf, sizeof buf, stdin)) {}
            exit(0);
        });

        dispatch_main();
    }
    return 0;
}
