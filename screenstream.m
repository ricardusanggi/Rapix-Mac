// screenstream.m — merekam satu layar dan mengirimnya sebagai video H.264 ke stdout.
// Memakai ScreenCaptureKit (rekam layar) + VideoToolbox (encoder hardware), jadi 60 fps
// tanpa membebani CPU seperti JPEG per frame.
//
// Pemakaian: ./screenstream DISPLAY_ID [fps] [bitrate_mbps]
//
// stdout: rangkaian paket [panjang 4 byte big-endian][flag 1 byte: 1 = keyframe][data H.264 Annex-B]
// stdin : baris "k" meminta keyframe (tablet baru tersambung); stdin ditutup = berhenti.
// Keluar dengan kode 3 saat resolusi layar berubah, supaya server menjalankan ulang dengan ukuran baru.

#import <Foundation/Foundation.h>
#import <CoreGraphics/CoreGraphics.h>
#import <CoreMedia/CoreMedia.h>
#import <ScreenCaptureKit/ScreenCaptureKit.h>
#import <VideoToolbox/VideoToolbox.h>

static VTCompressionSessionRef gSession;
static dispatch_queue_t gCaptureQueue;
static CVPixelBufferRef gLast;          // frame terakhir, dipakai ulang saat keyframe diminta ketika layar diam
static CMTime gLastPts;
static volatile BOOL gForceKey = YES;
static size_t gWidth, gHeight;
static CGDirectDisplayID gDisplayID;

static void writePacket(const uint8_t *data, size_t len, BOOL key) {
    uint8_t head[5];
    uint32_t n = (uint32_t)len + 1;
    head[0] = n >> 24; head[1] = n >> 16; head[2] = n >> 8; head[3] = n;
    head[4] = key ? 1 : 0;
    if (fwrite(head, 1, 5, stdout) != 5 || fwrite(data, 1, len, stdout) != len) exit(0);  // server berhenti
    fflush(stdout);
}

static void appendStartCode(NSMutableData *out, const uint8_t *nal, size_t len) {
    static const uint8_t start[4] = {0, 0, 0, 1};
    [out appendBytes:start length:4];
    [out appendBytes:nal length:len];
}

// VideoToolbox menghasilkan format AVCC (awalan panjang); browser butuh Annex-B (awalan 00 00 00 01)
static void encoded(void *ref, void *frameRef, OSStatus status, VTEncodeInfoFlags flags, CMSampleBufferRef sb) {
    if (status != noErr || !sb || !CMSampleBufferDataIsReady(sb)) return;

    BOOL key = YES;
    CFArrayRef att = CMSampleBufferGetSampleAttachmentsArray(sb, false);
    if (att && CFArrayGetCount(att) > 0) {
        CFDictionaryRef d = CFArrayGetValueAtIndex(att, 0);
        key = !CFDictionaryContainsKey(d, kCMSampleAttachmentKey_NotSync);
    }

    NSMutableData *out = [NSMutableData data];
    if (key) {  // sertakan SPS/PPS supaya tablet bisa mulai dari keyframe mana pun
        CMFormatDescriptionRef fmt = CMSampleBufferGetFormatDescription(sb);
        size_t count = 0;
        CMVideoFormatDescriptionGetH264ParameterSetAtIndex(fmt, 0, NULL, NULL, &count, NULL);
        for (size_t i = 0; i < count; i++) {
            const uint8_t *ps; size_t psLen;
            if (CMVideoFormatDescriptionGetH264ParameterSetAtIndex(fmt, i, &ps, &psLen, NULL, NULL) == noErr)
                appendStartCode(out, ps, psLen);
        }
    }

    CMBlockBufferRef block = CMSampleBufferGetDataBuffer(sb);
    size_t total = 0; char *ptr = NULL;
    if (CMBlockBufferGetDataPointer(block, 0, NULL, &total, &ptr) != noErr) return;
    size_t off = 0;
    while (off + 4 <= total) {
        uint32_t len = ((uint8_t)ptr[off] << 24) | ((uint8_t)ptr[off + 1] << 16) | ((uint8_t)ptr[off + 2] << 8) | (uint8_t)ptr[off + 3];
        off += 4;
        if (off + len > total) break;
        appendStartCode(out, (const uint8_t *)ptr + off, len);
        off += len;
    }
    writePacket(out.bytes, out.length, key);
}

static void encodeBuffer(CVPixelBufferRef pb, CMTime pts) {
    NSDictionary *props = nil;
    if (gForceKey) {
        gForceKey = NO;
        props = @{(__bridge NSString *)kVTEncodeFrameOptionKey_ForceKeyFrame: @YES};
    }
    VTCompressionSessionEncodeFrame(gSession, pb, pts, kCMTimeInvalid, (__bridge CFDictionaryRef)props, NULL, NULL);
}

static BOOL createSession(int fps, int mbps) {
    NSDictionary *spec = @{(__bridge NSString *)kVTVideoEncoderSpecification_EnableLowLatencyRateControl: @YES};
    OSStatus st = VTCompressionSessionCreate(NULL, (int32_t)gWidth, (int32_t)gHeight, kCMVideoCodecType_H264,
                                             (__bridge CFDictionaryRef)spec, NULL, NULL, encoded, NULL, &gSession);
    if (st != noErr)  // mode latensi rendah tidak tersedia: pakai encoder biasa
        st = VTCompressionSessionCreate(NULL, (int32_t)gWidth, (int32_t)gHeight, kCMVideoCodecType_H264,
                                        NULL, NULL, NULL, encoded, NULL, &gSession);
    if (st != noErr) return NO;

    VTSessionSetProperty(gSession, kVTCompressionPropertyKey_RealTime, kCFBooleanTrue);
    VTSessionSetProperty(gSession, kVTCompressionPropertyKey_AllowFrameReordering, kCFBooleanFalse);  // tanpa B-frame: latensi rendah
    VTSessionSetProperty(gSession, kVTCompressionPropertyKey_ProfileLevel, kVTProfileLevel_H264_High_AutoLevel);
    VTSessionSetProperty(gSession, kVTCompressionPropertyKey_ExpectedFrameRate, (__bridge CFNumberRef)@(fps));
    VTSessionSetProperty(gSession, kVTCompressionPropertyKey_AverageBitRate, (__bridge CFNumberRef)@(mbps * 1000000));
    VTSessionSetProperty(gSession, kVTCompressionPropertyKey_MaxKeyFrameInterval, (__bridge CFNumberRef)@(fps * 10));
    VTCompressionSessionPrepareToEncodeFrames(gSession);
    return YES;
}

@interface Output : NSObject <SCStreamOutput, SCStreamDelegate>
@end

@implementation Output
- (void)stream:(SCStream *)stream didOutputSampleBuffer:(CMSampleBufferRef)sb ofType:(SCStreamOutputType)type {
    if (type != SCStreamOutputTypeScreen || !CMSampleBufferIsValid(sb)) return;
    CFArrayRef att = CMSampleBufferGetSampleAttachmentsArray(sb, false);
    if (!att || CFArrayGetCount(att) == 0) return;
    CFDictionaryRef info = CFArrayGetValueAtIndex(att, 0);
    NSNumber *status = (__bridge NSNumber *)CFDictionaryGetValue(info, (__bridge CFStringRef)SCStreamFrameInfoStatus);
    if (status.integerValue != SCFrameStatusComplete) return;  // layar diam: tidak ada frame baru

    CVPixelBufferRef pb = CMSampleBufferGetImageBuffer(sb);
    if (!pb) return;

    CGDisplayModeRef mode = CGDisplayCopyDisplayMode(gDisplayID);
    if (mode) {
        size_t w = CGDisplayModeGetPixelWidth(mode), h = CGDisplayModeGetPixelHeight(mode);
        CGDisplayModeRelease(mode);
        if (w != gWidth || h != gHeight) exit(3);
    }

    if (gLast) CVPixelBufferRelease(gLast);
    gLast = CVPixelBufferRetain(pb);
    gLastPts = CMSampleBufferGetPresentationTimeStamp(sb);
    encodeBuffer(pb, gLastPts);
}

- (void)stream:(SCStream *)stream didStopWithError:(NSError *)error {
    fprintf(stderr, "rekaman berhenti: %s\n", error.localizedDescription.UTF8String);
    exit(4);
}
@end

static SCStream *gStream;
static Output *gOutput;

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc < 2) {
            fprintf(stderr, "pemakaian: screenstream DISPLAY_ID [fps] [bitrate_mbps]\n");
            return 1;
        }
        gDisplayID = (CGDirectDisplayID)strtoul(argv[1], NULL, 10);
        int fps = argc > 2 ? atoi(argv[2]) : 60;
        int mbps = argc > 3 ? atoi(argv[3]) : 20;
        signal(SIGPIPE, SIG_IGN);

        CGDisplayModeRef mode = CGDisplayCopyDisplayMode(gDisplayID);
        if (!mode) {
            fprintf(stderr, "layar %u tidak ditemukan\n", gDisplayID);
            return 2;
        }
        gWidth = CGDisplayModeGetPixelWidth(mode);
        gHeight = CGDisplayModeGetPixelHeight(mode);
        CGDisplayModeRelease(mode);

        if (!createSession(fps, mbps)) {
            fprintf(stderr, "encoder H.264 tidak tersedia\n");
            return 5;
        }
        gCaptureQueue = dispatch_queue_create("rapix.capture", DISPATCH_QUEUE_SERIAL);

        [SCShareableContent getShareableContentExcludingDesktopWindows:NO onScreenWindowsOnly:YES
                                                     completionHandler:^(SCShareableContent *content, NSError *error) {
            SCDisplay *display = nil;
            for (SCDisplay *d in content.displays)
                if (d.displayID == gDisplayID) display = d;
            if (!display) {
                fprintf(stderr, "tidak bisa merekam layar (cek izin Screen Recording): %s\n",
                        error ? error.localizedDescription.UTF8String : "layar tidak ada");
                exit(6);
            }
            SCContentFilter *filter = [[SCContentFilter alloc] initWithDisplay:display excludingWindows:@[]];
            SCStreamConfiguration *cfg = [[SCStreamConfiguration alloc] init];
            cfg.width = gWidth;
            cfg.height = gHeight;
            cfg.minimumFrameInterval = CMTimeMake(1, fps);
            cfg.pixelFormat = kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange;  // format asli encoder, tanpa konversi
            cfg.showsCursor = YES;
            cfg.queueDepth = 3;  // minimum yang dianjurkan; tiap buffer frame Retina ~6 MB, dikali jumlah layar

            gOutput = [[Output alloc] init];
            gStream = [[SCStream alloc] initWithFilter:filter configuration:cfg delegate:gOutput];
            NSError *addErr = nil;
            [gStream addStreamOutput:gOutput type:SCStreamOutputTypeScreen sampleHandlerQueue:gCaptureQueue error:&addErr];
            [gStream startCaptureWithCompletionHandler:^(NSError *startErr) {
                if (startErr) {
                    fprintf(stderr, "tidak bisa merekam layar: %s\n", startErr.localizedDescription.UTF8String);
                    exit(6);
                }
                fprintf(stderr, "READY %zux%zu %d fps %d Mbps\n", gWidth, gHeight, fps, mbps);
            }];
        }];

        // perintah dari server: "k" = kirim keyframe sekarang (juga saat layar sedang diam)
        dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
            char buf[64];
            while (fgets(buf, sizeof buf, stdin)) {
                if (buf[0] == 'k') {
                    dispatch_async(gCaptureQueue, ^{
                        gForceKey = YES;
                        if (gLast) {
                            gLastPts = CMTimeAdd(gLastPts, CMTimeMake(1, 600));
                            encodeBuffer(gLast, gLastPts);
                        }
                    });
                }
            }
            exit(0);
        });

        dispatch_main();
    }
    return 0;
}
