import android.media.AudioRecord
import android.media.MediaRecorder
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.FileOutputStream
import okhttp3.MediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody

class VoiceDefiner {
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
    
    fun defineVoice(description: String, audioFile: File, callback: (Result<String>) -> Unit) {
        Thread {
            try {
                val client = OkHttpClient()
                val requestBody = MultipartBody.Builder()
                    .setType(MultipartBody.FORM)
                    .addFormDataPart("session_id", java.util.UUID.randomUUID().toString())
                    .addFormDataPart("description", description)
                    .addFormDataPart(
                        "audio",
                        audioFile.name,
                        RequestBody.create(MediaType.parse("audio/*"), audioFile)
                    )
                    .build()
                
                val request = Request.Builder()
                    .url("http://localhost:8000/api/voice/define")
                    .post(requestBody)
                    .build()
                
                val response = client.newCall(request).execute()
                val responseBody = response.body()?.string()
                
                if (response.isSuccessful && responseBody != null) {
                    // 解析JSON响应
                    val json = org.json.JSONObject(responseBody)
                    if (json.has("voice_definition")) {
                        val voiceDefinition = json.getString("voice_definition")
                        callback(Result.success(voiceDefinition))
                    } else if (json.has("error")) {
                        val error = json.getString("error")
                        callback(Result.failure(Exception(error)))
                    } else {
                        callback(Result.failure(Exception("Invalid response")))
                    }
                } else {
                    callback(Result.failure(Exception("Request failed: ${response.code()}")))
                }
            } catch (e: Exception) {
                callback(Result.failure(e))
            }
        }.start()
    }
}
