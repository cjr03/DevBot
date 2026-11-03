// Finds a Roam invitation or a knock on screen by reading Roam's windows (Roam hides its buttons from Accessibility) and clicks Accept or Admit.
// Arguments: invitation texts to skip (already clicked). Prints the invitation text when one was accepted, "noperm" without Screen Recording permission, nothing otherwise.
import AppKit
import ScreenCaptureKit
import Vision

_ = NSApplication.shared  // connects to the window server, which ScreenCaptureKit needs
if !CGPreflightScreenCaptureAccess() { print("noperm"); exit(0) }
let content = try await SCShareableContent.excludingDesktopWindows(true, onScreenWindowsOnly: true)
for w in content.windows where w.owningApplication?.applicationName == "Roam" {
    let cfg = SCStreamConfiguration()
    cfg.width = Int(w.frame.width) * 2; cfg.height = Int(w.frame.height) * 2
    guard let img: CGImage = try? await SCScreenshotManager.captureImage(contentFilter: SCContentFilter(desktopIndependentWindow: w), configuration: cfg) else { continue }
    let req = VNRecognizeTextRequest()
    try? VNImageRequestHandler(cgImage: img).perform([req])
    let lines = (req.results ?? []).compactMap { o in o.topCandidates(1).first.map { ($0.string, o.boundingBox) } }
    let seen = CommandLine.arguments.dropFirst()
    func norm(_ s: String) -> String {
        s.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            .trimmingCharacters(in: CharacterSet(charactersIn: "!.… "))
    }
    // the button sits under the banner, or on the same row to its right. A chat line must not steer the click.
    func press(_ anchor: (String, CGRect), _ labels: [String]) -> (String, CGRect)? {
        for label in labels {
            let hits = lines.compactMap { item -> (String, CGRect, CGFloat)? in
                let t = norm(item.0)
                guard t == label || (label == "let into" && t.hasPrefix("let into")) else { return nil }
                let below = anchor.1.minY - item.1.midY
                let under = below >= -0.02 && below < 0.2
                let beside = abs(item.1.midY - anchor.1.midY) < 0.08 && item.1.minX >= anchor.1.minX
                guard under || beside else { return nil }
                return (item.0, item.1, abs(item.1.midY - anchor.1.midY) + abs(item.1.midX - anchor.1.midX))
            }
            if let best = hits.min(by: { $0.2 < $1.2 }) { return (best.0, best.1) }
        }
        return nil
    }
    let hit: (String, CGRect, String)? = {
        // a knock or a waiting guest wins over a stale invite card still sitting in the chat
        if let knock = lines.first(where: { norm($0.0).contains("knock") && !seen.contains($0.0) }),
           let btn = press(knock, ["come on in", "admit", "accept"]) { return (knock.0, btn.1, "admit:" + knock.0) }
        if let wait = lines.first(where: { let t = norm($0.0); return (t.contains("waiting to join") || t.contains("waiting for entry")) && !seen.contains($0.0) }),
           let btn = press(wait, ["admit all", "admit", "let into"]) { return (wait.0, btn.1, "admit:" + wait.0) }
        if let invite = lines.first(where: { $0.0.contains("invited you") && !seen.contains($0.0) }),
           let btn = press(invite, ["accept", "join"]) { return (invite.0, btn.1, invite.0) }
        return nil
    }()
    guard let hit else { continue }
    // Vision boxes are normalized with the origin bottom-left; window frames are in points, origin top-left
    let r = w.frame
    let p = CGPoint(x: r.minX + hit.1.midX * r.width, y: r.minY + (1 - hit.1.midY) * r.height)
    NSRunningApplication(processIdentifier: w.owningApplication!.processID)?.activate()
    for t in [CGEventType.mouseMoved, .leftMouseDown, .leftMouseUp] {  // Roam ignores a click on an inactive window
        usleep(300_000)
        CGEvent(mouseEventSource: nil, mouseType: t, mouseCursorPosition: p, mouseButton: .left)?.post(tap: .cghidEventTap)
    }
    print(hit.2)
    exit(0)
}
