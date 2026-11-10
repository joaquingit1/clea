// Captures one of the CALLING process's own windows (frame, traffic lights and vibrancy included) to a PNG.
// A process may image its own windows without the Screen Recording permission, which the terminal running the
// harness does not have (screencapture and desktopCapturer are refused). Node-API, so one build works in any Electron.
#include <node_api.h>
#import <Cocoa/Cocoa.h>
#import <ImageIO/ImageIO.h>
#import <UniformTypeIdentifiers/UniformTypeIdentifiers.h>

#include <dlfcn.h>
// resolved at runtime: the SDK marks the CoreGraphics one obsolete and the SkyLight one is private
typedef CGImageRef (*ListCreateImage)(CGRect, uint32_t, uint32_t, uint32_t);
typedef int (*MainConnection)(void);
typedef CFArrayRef (*HWCapture)(int, uint32_t *, int, uint32_t);

static napi_value Capture(napi_env env, napi_callback_info info) {
  size_t argc = 2; napi_value argv[2];
  napi_get_cb_info(env, info, &argc, argv, NULL, NULL);
  uint32_t wid = 0; napi_get_value_uint32(env, argv[0], &wid);
  char path[4096]; size_t len = 0; napi_get_value_string_utf8(env, argv[1], path, sizeof path, &len);
  CGImageRef img = NULL; const char *how = "none";
  void *sky = dlopen("/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight", RTLD_LAZY);
  MainConnection conn = sky ? (MainConnection)dlsym(sky, "SLSMainConnectionID") : NULL;
  HWCapture hw = sky ? (HWCapture)dlsym(sky, "SLSHWCaptureWindowList") : NULL;
  ListCreateImage cg = (ListCreateImage)dlsym(RTLD_DEFAULT, "CGWindowListCreateImage");
  if (hw && conn) {
    CFArrayRef arr = hw(conn(), &wid, 1, (1 << 11) /* best resolution */ | (1 << 0) /* ignore global clip */);
    if (arr && CFArrayGetCount(arr) > 0) { img = (CGImageRef)CFRetain(CFArrayGetValueAtIndex(arr, 0)); how = "skylight"; }
    if (arr) CFRelease(arr);
  }
  if (!img && cg) {
    img = cg(CGRectNull, 1 << 3 /* including window */, wid, 1 << 3 /* best resolution */);
    how = "cgwindowlist";
  }
  napi_value out;
  if (!img) { napi_create_string_utf8(env, "error: no image", NAPI_AUTO_LENGTH, &out); return out; }
  NSURL *url = [NSURL fileURLWithPath:[NSString stringWithUTF8String:path]];
  CGImageDestinationRef dst = CGImageDestinationCreateWithURL((__bridge CFURLRef)url, (__bridge CFStringRef)UTTypePNG.identifier, 1, NULL);
  CGImageDestinationAddImage(dst, img, NULL);
  bool ok = CGImageDestinationFinalize(dst);
  char msg[256]; snprintf(msg, sizeof msg, "%s %s %zux%zu", ok ? "ok" : "error: write", how, CGImageGetWidth(img), CGImageGetHeight(img));
  CFRelease(dst); CGImageRelease(img);
  napi_create_string_utf8(env, msg, NAPI_AUTO_LENGTH, &out);
  return out;
}

NAPI_MODULE_INIT() {
  napi_value fn;
  napi_create_function(env, "capture", NAPI_AUTO_LENGTH, Capture, NULL, &fn);
  napi_set_named_property(env, exports, "capture", fn);
  return exports;
}
