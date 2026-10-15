import UIKit
import AVFoundation

class VoiceDefiner: NSObject, AVAudioRecorderDelegate {
    private var audioRecorder: AVAudioRecorder?
    private var audioFileURL: URL
    private var isRecording = false
    
    var onRecordingComplete: ((Data) -> Void)?
    
    override init() {
        let tempDir = NSTemporaryDirectory()
        audioFileURL = URL(fileURLWithPath: tempDir).appendingPathComponent("voice_reference.caf")
        super.init()
    }
    
    func startRecording() {
        let settings = [
            AVFormatIDKey: Int(kAudioFormatLinearPCM),
            AVSampleRateKey: 44100,
            AVNumberOfChannelsKey: 1,
            AVLinearPCMBitDepthKey: 16,
            AVLinearPCMIsBigEndianKey: false,
            AVLinearPCMIsFloatKey: false
        ]
        
        do {
            try AVAudioSession.sharedInstance().setCategory(.playAndRecord, mode: .default)
            try AVAudioSession.sharedInstance().setActive(true)
            
            audioRecorder = try AVAudioRecorder(url: audioFileURL, settings: settings)
            audioRecorder?.delegate = self
            audioRecorder?.record()
            isRecording = true
        } catch {
            print("Recording failed: \(error)")
        }
    }
    
    func stopRecording() {
        audioRecorder?.stop()
        isRecording = false
    }
    
    func defineVoice(description: String, completion: @escaping (Result<String, Error>) -> Void) {
        guard let audioData = try? Data(contentsOf: audioFileURL) else {
            completion(.failure(NSError(domain: "VoiceDefiner", code: 1, userInfo: [NSLocalizedDescriptionKey: "Failed to read audio file"])))
            return
        }
        
        // 调用API定义语音
        let url = URL(string: "http://localhost:8000/api/voice/define")!
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        
        let boundary = "Boundary-\(UUID().uuidString)"
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        
        let sessionId = UUID().uuidString
        
        var body = Data()
        
        // 添加session_id字段
        body.append("--\(boundary)\r\n".data(using: .utf8)!)
        body.append("Content-Disposition: form-data; name=\"session_id\"\r\n\r\n".data(using: .utf8)!)
        body.append("\(sessionId)\r\n".data(using: .utf8)!)
        
        // 添加description字段
        body.append("--\(boundary)\r\n".data(using: .utf8)!)
        body.append("Content-Disposition: form-data; name=\"description\"\r\n\r\n".data(using: .utf8)!)
        body.append("\(description)\r\n".data(using: .utf8)!)
        
        // 添加audio文件
        body.append("--\(boundary)\r\n".data(using: .utf8)!)
        body.append("Content-Disposition: form-data; name=\"audio\"; filename=\"voice_reference.caf\"\r\n".data(using: .utf8)!)
        body.append("Content-Type: audio/x-caf\r\n\r\n".data(using: .utf8)!)
        body.append(audioData)
        body.append("\r\n".data(using: .utf8)!)
        
        body.append("--\(boundary)--\r\n".data(using: .utf8)!)
        
        request.httpBody = body
        
        let task = URLSession.shared.dataTask(with: request) { data, response, error in
            if let error = error {
                completion(.failure(error))
                return
            }
            
            guard let data = data else {
                completion(.failure(NSError(domain: "VoiceDefiner", code: 2, userInfo: [NSLocalizedDescriptionKey: "No data received"])))
                return
            }
            
            do {
                if let json = try JSONSerialization.jsonObject(with: data, options: []) as? [String: Any] {
                    if let voiceDefinition = json["voice_definition"] as? String {
                        completion(.success(voiceDefinition))
                    } else if let error = json["error"] as? String {
                        completion(.failure(NSError(domain: "VoiceDefiner", code: 3, userInfo: [NSLocalizedDescriptionKey: error])))
                    } else {
                        completion(.failure(NSError(domain: "VoiceDefiner", code: 4, userInfo: [NSLocalizedDescriptionKey: "Invalid response"])))
                    }
                }
            } catch {
                completion(.failure(error))
            }
        }
        
        task.resume()
    }
    
    func audioRecorderDidFinishRecording(_ recorder: AVAudioRecorder, successfully flag: Bool) {
        if flag {
            if let audioData = try? Data(contentsOf: audioFileURL) {
                onRecordingComplete?(audioData)
            }
        } else {
            print("Recording failed")
        }
    }
}
