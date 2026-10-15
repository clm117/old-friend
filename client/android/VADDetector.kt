// 使用WebRTC VAD进行语音活动检测
class VADDetector {
    private val vad: WebRtcVad
    private val sampleRate: Int = 44100
    private val frameSize: Int = 160 // 10ms frame at 16kHz
    
    init {
        vad = WebRtcVad()
        vad.init()
        vad.setMode(1) // 0-3, 3 is most aggressive
    }
    
    fun detectVoice(audioData: ByteArray): Boolean {
        // 转换为16kHz采样率
        val resampledData = resampleTo16kHz(audioData)
        
        // 按帧处理
        val frameCount = resampledData.size / (frameSize * 2) // 2 bytes per sample
        
        for (i in 0 until frameCount) {
            val startIndex = i * frameSize * 2
            val endIndex = startIndex + frameSize * 2
            val frameData = ByteArray(endIndex - startIndex)
            System.arraycopy(resampledData, startIndex, frameData, 0, frameData.size)
            
            val samples = ShortArray(frameSize)
            for (j in 0 until frameSize) {
                samples[j] = (frameData[j * 2].toInt() shl 8 or (frameData[j * 2 + 1].toInt() and 0xFF)).toShort()
            }
            
            val result = vad.process(sampleRate, samples, frameSize)
            if (result == 1) {
                return true
            }
        }
        
        return false
    }
    
    private fun resampleTo16kHz(audioData: ByteArray): ByteArray {
        // 这里简化实现，实际需要使用WebRTC的重采样器
        return audioData
    }
}

// WebRTC VAD包装类
class WebRtcVad {
    init {
        // 加载WebRTC VAD库
        System.loadLibrary("webrtc_vad")
    }
    
    external fun init(): Int
    external fun setMode(mode: Int): Int
    external fun process(sampleRate: Int, samples: ShortArray, length: Int): Int
}
