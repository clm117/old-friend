// 使用WebRTC VAD进行语音活动检测
class VADDetector {
    private let vad = WebRTCVAD()
    private let sampleRate: Int = 44100
    private let frameSize: Int = 160 // 10ms frame at 16kHz
    
    init() {
        vad.setMode(1) // 0-3, 3 is most aggressive
    }
    
    func detectVoice(audioData: Data) -> Bool {
        // 转换为16kHz采样率
        let resampledData = resampleTo16kHz(audioData: audioData)
        
        // 按帧处理
        let frameCount = resampledData.count / (frameSize * 2) // 2 bytes per sample
        
        for i in 0..<frameCount {
            let startIndex = i * frameSize * 2
            let endIndex = startIndex + frameSize * 2
            let frameData = resampledData.subdata(in: startIndex..<endIndex)
            
            let result = vad.process(sampleRate, frameData)
            if result == 1 {
                return true
            }
        }
        
        return false
    }
    
    private func resampleTo16kHz(audioData: Data) -> Data {
        // 这里简化实现，实际需要使用WebRTC的重采样器
        // 或者使用AVAudioEngine进行重采样
        return audioData
    }
}

// WebRTC VAD包装类
class WebRTCVAD {
    private let vad: OpaquePointer
    
    init() {
        vad = WebRtcVad_Create()
    }
    
    deinit {
        WebRtcVad_Free(vad)
    }
    
    func setMode(_ mode: Int32) {
        WebRtcVad_Init(vad)
        WebRtcVad_set_mode(vad, mode)
    }
    
    func process(_ sampleRate: Int, _ audioData: Data) -> Int32 {
        return audioData.withUnsafeBytes { pointer -> Int32 in
            let samples = pointer.bindMemory(to: Int16.self)
            return WebRtcVad_Process(vad, Int32(sampleRate), samples.baseAddress, Int32(audioData.count / 2))
        }
    }
}

// WebRTC VAD C函数声明
extern func WebRtcVad_Create() -> OpaquePointer
extern func WebRtcVad_Free(_ vad: OpaquePointer)
extern func WebRtcVad_Init(_ vad: OpaquePointer) -> Int32
extern func WebRtcVad_set_mode(_ vad: OpaquePointer, _ mode: Int32) -> Int32
extern func WebRtcVad_Process(_ vad: OpaquePointer, _ fs: Int32, _ data: UnsafePointer<Int16>?, _ len: Int32) -> Int32
