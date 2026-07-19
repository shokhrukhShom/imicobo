import SwiftUI

struct ContentView: View {
    @StateObject private var mic = MicManager()
    @State private var showSettings = false
    @State private var showEffects = false
    @State private var typedCode = ""
    @AppStorage("lastCode") private var lastCode = ""     // prefilled for quick reconnect
    @FocusState private var codeFocused: Bool

    var body: some View {
        NavigationStack {
            ZStack {
                Color(red: 0.04, green: 0.05, blue: 0.06).ignoresSafeArea()
                VStack(spacing: 28) {
                    Spacer()
                    content
                    Spacer()
                }
                .padding(28)
            }
            .navigationTitle("iMicobo")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button { showSettings = true } label: { Image(systemName: "gearshape") }
                }
                ToolbarItem(placement: .topBarLeading) {
                    Button { showEffects = true } label: { Image(systemName: "slider.horizontal.3") }
                        .disabled(mic.phase != .live)
                }
            }
            .sheet(isPresented: $showSettings) { SettingsView(mic: mic) }
            .sheet(isPresented: $showEffects)  { EffectsView(mic: mic) }
            .preferredColorScheme(.dark)
        }
    }

    @ViewBuilder
    private var content: some View {
        switch mic.phase {

        case .idle:
            Image(systemName: "mic.circle.fill")
                .font(.system(size: 88)).foregroundStyle(Color.mint)
            Text("Turn this iPhone into a microphone for any screen.")
                .font(.callout).foregroundStyle(.secondary)
                .multilineTextAlignment(.center)

            Button { mic.startScanning() } label: {
                Label("Scan the QR code", systemImage: "qrcode.viewfinder")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent).controlSize(.large).tint(.mint)

            Button("Enter code instead") { mic.findAndPair() }
                .buttonStyle(.plain)
                .font(.subheadline)
                .foregroundStyle(Color.mint)

        case .scanning:
            ScannerView(
                onFound: { mic.handleScan($0) },
                onError: { mic.phase = .failed($0) }
            )
            .frame(height: 380)
            .clipShape(RoundedRectangle(cornerRadius: 20))
            Text("Point the camera at the code on your screen")
                .font(.callout).foregroundStyle(.secondary)
            Button("Cancel") { mic.phase = .idle }
                .buttonStyle(.bordered).controlSize(.large)

        case .searching:
            ProgressView().controlSize(.large)
            Text("Connecting to iMicobo…")
                .font(.callout).foregroundStyle(.secondary)

        case .enterCode:
            Image(systemName: "keyboard")
                .font(.system(size: 44)).foregroundStyle(Color.mint)
            Text("Enter the code from your screen")
                .font(.headline)
            Text("It's showing on imicobo.com.")
                .font(.caption).foregroundStyle(.secondary)

            TextField("0000", text: $typedCode)
                .keyboardType(.numberPad)
                .focused($codeFocused)
                .multilineTextAlignment(.center)
                .font(.system(size: 44, weight: .bold, design: .monospaced))
                .tracking(8)
                .padding(.vertical, 14)
                .background(Color.gray.opacity(0.15))
                .clipShape(RoundedRectangle(cornerRadius: 14))
                .onChange(of: typedCode) { _, v in
                    typedCode = String(v.filter(\.isNumber).prefix(6))
                }

            if let err = mic.codeError {
                Text(err).font(.caption).foregroundStyle(Color.orange)
            }

            HStack(spacing: 12) {
                Button("Cancel") { mic.cancel(); typedCode = "" }
                    .buttonStyle(.bordered).controlSize(.large)
                Button("Connect") { mic.submit(code: typedCode) }
                    .buttonStyle(.borderedProminent).controlSize(.large).tint(.mint)
                    .disabled(typedCode.count < 4)
            }
            .onAppear {
                if typedCode.isEmpty { typedCode = lastCode }   // one-tap reconnect
                codeFocused = true
            }

        case .connecting:
            ProgressView().controlSize(.large)
            Text("Connecting…").font(.callout).foregroundStyle(.secondary)

        case .live:
            Button { mic.muted.toggle() } label: {
                ZStack {
                    Circle()
                        .fill(mic.muted ? Color.gray.opacity(0.25) : Color.mint.opacity(0.2))
                        .frame(width: 190, height: 190)
                    Image(systemName: mic.muted ? "mic.slash.fill" : "mic.fill")
                        .font(.system(size: 70))
                        .foregroundStyle(mic.muted ? Color.gray : Color.mint)
                }
            }
            Text(mic.muted ? "Muted" : "Live — you're on the screen")
                .font(.headline)
                .foregroundStyle(mic.muted ? Color.gray : Color.mint)
            Text("Tap to \(mic.muted ? "unmute" : "mute") · keeps running with the screen off")
                .font(.caption).foregroundStyle(.secondary)
                .multilineTextAlignment(.center)

        case .failed(let msg):
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.system(size: 48)).foregroundStyle(.orange)
            Text(msg).font(.callout).foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            Button("Try again") { mic.findAndPair() }
                .buttonStyle(.borderedProminent).controlSize(.large).tint(.mint)
        }
    }
}

struct SettingsView: View {
    @ObservedObject var mic: MicManager
    @Environment(\.dismiss) private var dismiss
    @AppStorage("savedRelay") private var savedRelay = ""

    var body: some View {
        NavigationStack {
            Form {
                Section("Connection") {
                    LabeledContent("Screen", value: savedRelay.isEmpty ? "None saved" : savedRelay)
                    Button("Forget this screen", role: .destructive) { savedRelay = "" }
                        .disabled(savedRelay.isEmpty)
                    Button("Disconnect", role: .destructive) { mic.cancel(); dismiss() }
                }
                Section("Audio") {
                    NavigationLink("Effects") { EffectsView(mic: mic) }
                }
            }
            .navigationTitle("Settings")
            .toolbar { ToolbarItem(placement: .confirmationAction) {
                Button("Done") { dismiss() } } }
        }
    }
}

#Preview { ContentView() }
