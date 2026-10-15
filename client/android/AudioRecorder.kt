import android.media.AudioRecord
import android.media.MediaRecorder
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.FileOutputStream

class AudioRecorder {
    private var audioRecord: AudioRecord? = null
    private var isRecording = false
    private val bufferSize = AudioRecord.getMinBufferSize(44100, android.media.AudioFormat.CHANNEL_IN_MONO, android.media.AudioFormat.ENCODING_PCM_16BIT)
    private val outputStream = ByteArrayOutputStream()
    
    fun startRecording() {
        audioRecord = AudioRecord(MediaRecorder.AudioSource.MIC, 44100, android.media.AudioFormat.CHANNEL_IN_MONO, android.media.AudioFormat.ENCODING_PCM_16BIT, bufferSize)
        audioRecord?.startRecording()
        isRecording = true
        
        // 启动录音线程
        Thread {
            val buffer = ByteArray(bufferSize)
            while (isRecording) {
                val read = audioRecord?.read(buffer, 0, bufferSize) ?: 0
                if (read > 0) {
                    outputStream.write(buffer, 0, read)
                }
            }
        }.start()
    }
    
    fun stopRecording(): ByteArray {
        isRecording = false
        audioRecord?.stop()
        audioRecord?.release()
        return outputStream.toByteArray()
    }
    
    fun saveToFile(file: File): Boolean {
        return try {
            val data = stopRecording()
            val fos = FileOutputStream(file)
            fos.write(data)
            fos.close()
            true
        } catch (e: Exception) {
            e.printStackTrace()
            false
        }
    }
}
