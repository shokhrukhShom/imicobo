import Foundation
import Combine
import SwiftUI
import WebRTC
import AVFoundation

/// iMicobo — turns the iPhone into a wireless microphone for any screen
/// running the browser receiver, on the same Wi-Fi OR over the internet.
@MainActor
final class MicManager: NSObject, ObservableObject {

    enum Phase: Equatable {
        case idle
        case scanning           // camera open, looking for the QR
        case searching          // locating the relay (LAN scan / saved host)
        case enterCode          // user types the code shown on the screen
        case connecting
        case live
        case failed(String)
    }

    @Published var phase: Phase = .idle
    @Published var muted = false {
        didSet { audioTrack?.isEnabled = !muted; sendEffects() }
    }
    @Published var relayHost = ""          // "10.0.0.23:8080"  OR  "imicobo.com"
    @Published var codeError: String? = nil
    private var roomCode = ""              // the code the user typed

    /// Effect knobs. Every change is pushed to the browser over the data channel,
    /// which does the actual DSP (Web Audio) — the phone is just the control surface.
    @Published var fx = Effects.clean { didSet { sendEffects() } }

    /// Remembered relay — tried first so repeat sessions connect instantly.
    @AppStorage("savedRelay") private var savedRelay = ""

    private var factory: RTCPeerConnectionFactory!
    private var pc: RTCPeerConnection?
    private var audioTrack: RTCAudioTrack?
    private var fxChannel: RTCDataChannel?
    private var pollTask: Task<Void, Never>?

    private let config: RTCConfiguration = {
        let c = RTCConfiguration()
        // STUN lets the two peers discover their public addresses so the call
        // works across networks, not just one LAN. (Add a TURN server here too
        // if you need to punch through strict/cellular NATs.)
        c.iceServers = [ RTCIceServer(urlStrings: ["stun:stun.l.google.com:19302"]) ]
        c.sdpSemantics = .unifiedPlan
        return c
    }()

    override init() {
        super.init()
        RTCInitializeSSL()
        factory = RTCPeerConnectionFactory(
            encoderFactory: RTCDefaultVideoEncoderFactory(),
            decoderFactory: RTCDefaultVideoDecoderFactory())
        configureAudioSession()
    }

    // MARK: - URL scheme
    /// A LAN host carries a port ("10.0.0.5:8080") → plain http.
    /// A public host is a bare domain ("imicobo.com") → https (served by Caddy).
    /// This is what makes the SAME app work on localhost and on imicobo.com.
    private func base(_ host: String) -> String {
        host.contains(":") ? "http://\(host)" : "https://\(host)"
    }

    // MARK: - Audio session (also what keeps the mic alive with the screen off)
    private func configureAudioSession() {
        let s = RTCAudioSession.sharedInstance()
        s.lockForConfiguration()
        do {
            // .playAndRecord + the Background Modes > Audio capability = keeps
            // running when the screen locks.
            try s.setCategory(.playAndRecord, mode: .default,
                              options: [.defaultToSpeaker, .allowBluetooth])
            try s.setPreferredSampleRate(48000)
            try s.setPreferredIOBufferDuration(0.005)
            try s.setActive(true)
        } catch { print("audio session:", error) }
        s.unlockForConfiguration()
    }

    // MARK: - 1. Find the screen (saved relay first, then scan)
    func findAndPair() {
        phase = .searching
        if !savedRelay.isEmpty {
            Task {
                if await ping(savedRelay) {
                    relayHost = savedRelay
                    phase = .enterCode
                    return
                }
                scanForRelay()
            }
        } else {
            scanForRelay()
        }
    }

    private func scanForRelay() {
        Discovery.findRelay { [weak self] host in
            guard let self else { return }
            Task { @MainActor in
                guard let host else {
                    self.phase = .failed("No screen found. Open iMicobo in a browser on this Wi-Fi.")
                    return
                }
                self.savedRelay = host
                self.relayHost = host
                self.phase = .enterCode
            }
        }
    }

    private func ping(_ host: String) async -> Bool {
        guard let url = URL(string: "\(base(host))/ping") else { return false }
        var req = URLRequest(url: url); req.timeoutInterval = 1.5
        guard let (data, _) = try? await URLSession.shared.data(for: req),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return false }
        return obj["app"] as? String == "imicobo"
    }

    // MARK: - 1b. QR path — the code carries the host, so we skip discovery
    func startScanning() { phase = .scanning }

    /// Handle a scanned payload: imicobo://pair?h=<host>&c=<code>
    func handleScan(_ raw: String) {
        guard let comps = URLComponents(string: raw),
              comps.scheme == "imicobo",
              let host = comps.queryItems?.first(where: { $0.name == "h" })?.value,
              let code = comps.queryItems?.first(where: { $0.name == "c" })?.value,
              !host.isEmpty, !code.isEmpty else {
            phase = .failed("That doesn't look like an iMicobo code.")
            return
        }
        relayHost = host
        savedRelay = host
        submit(code: code)          // no typing, no scanning the LAN
    }

    // MARK: - 2. User types the code shown on the screen
    /// Validate the typed code against the relay, then start the call.
    func submit(code: String) {
        let c = code.trimmingCharacters(in: .whitespacesAndNewlines)
        codeError = nil
        Task {
            guard let url = URL(string: "\(base(relayHost))/join?code=\(c)") else { return }
            var req = URLRequest(url: url)
            req.httpMethod = "POST"
            guard let (data, resp) = try? await URLSession.shared.data(for: req),
                  let http = resp as? HTTPURLResponse else {
                codeError = "Couldn't reach the screen."
                phase = .enterCode
                return
            }
            if http.statusCode != 200 {
                let msg = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?
                    .flatMap { $0["error"] as? String }
                codeError = msg ?? "That code isn't valid."
                phase = .enterCode          // never strand the user on a spinner
                return
            }
            roomCode = c
            phase = .connecting
            await startCall()
        }
    }

    func cancel() {
        if !roomCode.isEmpty {
            Task { _ = try? await post("/cancel?code=\(roomCode)", body: nil) }
        }
        teardown()
        roomCode = ""
        phase = .idle
    }

    // MARK: - 4. WebRTC: send the offer, wait for the browser's answer
    private func startCall() async {
        guard let pc = factory.peerConnection(
            with: config,
            constraints: RTCMediaConstraints(mandatoryConstraints: nil, optionalConstraints: nil),
            delegate: self) else {
            phase = .failed("Couldn't start audio"); return
        }
        self.pc = pc

        // Raw-ish signal: no AEC/NS (they colour the voice and add delay).
        // AGC on so a quiet speaker still comes through — turn off if you clip.
        let constraints = RTCMediaConstraints(mandatoryConstraints: [
            "googEchoCancellation": "false",
            "googNoiseSuppression": "false",
            "googAutoGainControl":  "true",
            "googHighpassFilter":   "false"
        ], optionalConstraints: nil)

        let source = factory.audioSource(with: constraints)
        let track = factory.audioTrack(with: source, trackId: "mic0")
        track.isEnabled = !muted
        audioTrack = track
        pc.add(track, streamIds: ["mic"])

        // Control channel for the effect knobs (created before the offer so it's
        // negotiated in the same handshake).
        let cfg = RTCDataChannelConfiguration()
        cfg.isOrdered = true
        fxChannel = pc.dataChannel(forLabel: "fx", configuration: cfg)
        fxChannel?.delegate = self

        pc.offer(for: RTCMediaConstraints(mandatoryConstraints: nil, optionalConstraints: nil)) {
            [weak self] sdp, _ in
            guard let self, let sdp else { return }
            let tuned = RTCSessionDescription(type: sdp.type, sdp: Self.opus10ms(sdp.sdp))
            self.pc?.setLocalDescription(tuned) { _ in }   // published on ICE complete
        }
    }

    private func publishOffer() {
        guard let local = pc?.localDescription else { return }
        Task {
            _ = try? await post("/offer?code=\(roomCode)", body: encode(local))
            setBitrate(256)
            await awaitAnswer()
        }
    }

    private func awaitAnswer() async {
        for _ in 0..<60 {
            if case .live = phase { return }
            if let url = URL(string: "\(base(relayHost))/answer?code=\(roomCode)"),
               let (data, _) = try? await URLSession.shared.data(from: url),
               let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let sdpStr = obj["sdp"] as? String,
               let answer = decode(sdpStr) {
                pc?.setRemoteDescription(answer) { _ in }
                return
            }
            try? await Task.sleep(nanoseconds: 500_000_000)
        }
        phase = .failed("The screen never answered.")
    }

    /// Push knob positions + mute state to the browser.
    func sendEffects() {
        guard let ch = fxChannel, ch.readyState == .open,
              var obj = try? JSONSerialization.jsonObject(
                  with: JSONEncoder().encode(fx)) as? [String: Any] else { return }
        obj["muted"] = muted
        guard let data = try? JSONSerialization.data(withJSONObject: obj) else { return }
        ch.sendData(RTCDataBuffer(data: data, isBinary: false))
    }

    func apply(preset: Effects.Preset) { fx = preset.values }

    private func setBitrate(_ kbps: Int) {
        guard let sender = pc?.senders.first(where: { $0.track?.kind == "audio" }) else { return }
        let p = sender.parameters
        for e in p.encodings { e.maxBitrateBps = NSNumber(value: kbps * 1000) }
        sender.parameters = p
    }

    private func teardown() {
        pollTask?.cancel()
        fxChannel?.close(); fxChannel = nil
        pc?.close(); pc = nil; audioTrack = nil
    }

    // MARK: - helpers
    private func post(_ path: String, body: String?) async throws -> Data {
        guard let url = URL(string: "\(base(relayHost))\(path)") else { return Data() }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = body?.data(using: .utf8)
        let (data, _) = try await URLSession.shared.data(for: req)
        return data
    }

    /// Ask Opus for 10ms frames instead of the 20ms default.
    private static func opus10ms(_ sdp: String) -> String {
        guard let r = sdp.range(of: #"a=rtpmap:(\d+) opus"#, options: .regularExpression)
        else { return sdp }
        let pt = String(sdp[r]).replacingOccurrences(of: "a=rtpmap:", with: "")
            .components(separatedBy: " ").first ?? ""
        guard !pt.isEmpty else { return sdp }
        var out: [String] = []
        for line in sdp.components(separatedBy: "\r\n") {
            if line.hasPrefix("a=fmtp:\(pt) ") {
                var l = line
                if !l.contains("minptime=")    { l += ";minptime=10" }
                if !l.contains("useinbandfec=") { l += ";useinbandfec=1" }
                out.append(l); out.append("a=ptime:10")
            } else { out.append(line) }
        }
        return out.joined(separator: "\r\n")
    }

    private struct SDPJSON: Codable { let type: String; let sdp: String }

    private func encode(_ d: RTCSessionDescription) -> String {
        let t = d.type == .offer ? "offer" : "answer"
        let data = try? JSONEncoder().encode(SDPJSON(type: t, sdp: d.sdp))
        return data.flatMap { String(data: $0, encoding: .utf8) } ?? ""
    }

    private func decode(_ s: String) -> RTCSessionDescription? {
        guard let data = s.data(using: .utf8),
              let j = try? JSONDecoder().decode(SDPJSON.self, from: data) else { return nil }
        return RTCSessionDescription(type: j.type == "offer" ? .offer : .answer, sdp: j.sdp)
    }
}

extension MicManager: RTCPeerConnectionDelegate {
    nonisolated func peerConnection(_ pc: RTCPeerConnection, didChange s: RTCIceGatheringState) {
        if s == .complete { Task { @MainActor in self.publishOffer() } }
    }
    nonisolated func peerConnection(_ pc: RTCPeerConnection, didChange s: RTCIceConnectionState) {
        Task { @MainActor in
            switch s {
            case .connected, .completed: self.phase = .live
            case .failed:                self.phase = .failed("Connection lost")
            default: break
            }
        }
    }
    nonisolated func peerConnection(_ p: RTCPeerConnection, didChange s: RTCSignalingState) {}
    nonisolated func peerConnection(_ p: RTCPeerConnection, didAdd s: RTCMediaStream) {}
    nonisolated func peerConnection(_ p: RTCPeerConnection, didRemove s: RTCMediaStream) {}
    nonisolated func peerConnectionShouldNegotiate(_ p: RTCPeerConnection) {}
    nonisolated func peerConnection(_ p: RTCPeerConnection, didGenerate c: RTCIceCandidate) {}
    nonisolated func peerConnection(_ p: RTCPeerConnection, didRemove c: [RTCIceCandidate]) {}
    nonisolated func peerConnection(_ p: RTCPeerConnection, didOpen d: RTCDataChannel) {}
}


extension MicManager: RTCDataChannelDelegate {
    nonisolated func dataChannelDidChangeState(_ ch: RTCDataChannel) {
        if ch.readyState == .open {
            Task { @MainActor in self.sendEffects() }   // sync the browser immediately
        }
    }
    nonisolated func dataChannel(_ ch: RTCDataChannel, didReceiveMessageWith buffer: RTCDataBuffer) {}
}
