// The guard's human check for approvals made in the Clea app: a system Touch ID (or Mac password) dialog that
// shows what is about to be sent. Only a person can pass it, so the harness cannot approve its own asks.
// Usage: Clea "<reason>" [--cancel-title T] [--cancel-after N]   exit 0 = confirmed, 1 = declined/failed, 2 = unavailable
import Foundation
import LocalAuthentication

var args = Array(CommandLine.arguments.dropFirst())
var cancelAfter: Double? = nil
if let i = args.firstIndex(of: "--cancel-after"), i + 1 < args.count {
  cancelAfter = Double(args[i + 1])
  args.removeSubrange(i...(i + 1))
}
var cancelTitle = "Don't send"
if let i = args.firstIndex(of: "--cancel-title"), i + 1 < args.count {
  cancelTitle = args[i + 1]
  args.removeSubrange(i...(i + 1))
}
let reason = args.joined(separator: " ")
let ctx = LAContext()
ctx.localizedCancelTitle = cancelTitle
var err: NSError?
guard ctx.canEvaluatePolicy(.deviceOwnerAuthentication, error: &err) else {
  FileHandle.standardError.write("unavailable: \(err?.localizedDescription ?? "?")\n".data(using: .utf8)!)
  exit(2)
}
let done = DispatchSemaphore(value: 0)
var ok = false
ctx.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: reason) { success, e in
  ok = success
  if let e = e { FileHandle.standardError.write("\(e.localizedDescription)\n".data(using: .utf8)!) }
  done.signal()
}
if let s = cancelAfter { DispatchQueue.global().asyncAfter(deadline: .now() + s) { ctx.invalidate() } }
done.wait()
exit(ok ? 0 : 1)
