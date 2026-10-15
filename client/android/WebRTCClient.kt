import org.webrtc.PeerConnection
import org.webrtc.PeerConnectionFactory
import org.webrtc.AudioSource
import org.webrtc.AudioTrack

class WebRTCClient : PeerConnection.Observer {
    private var peerConnection: PeerConnection? = null
    private var audioTrack: AudioTrack? = null
    
    fun setup() {
        val factory = PeerConnectionFactory.builder().createPeerConnectionFactory()
        val configuration = PeerConnection.RTCConfiguration(listOf())
        
        peerConnection = factory.createPeerConnection(configuration, this)
        
        val audioSource = factory.createAudioSource(null)
        audioTrack = factory.createAudioTrack("audio", audioSource)
        peerConnection?.addTrack(audioTrack)
    }
    
    fun startCall() {
        peerConnection?.createOffer(object : PeerConnection.SdpObserver {
            override fun onCreateSuccess(sdp: org.webrtc.SessionDescription) {
                peerConnection?.setLocalDescription(object : PeerConnection.SdpObserver {
                    override fun onCreateSuccess(p0: org.webrtc.SessionDescription?) {}
                    override fun onSetSuccess() {}
                    override fun onCreateFailure(p0: String?) {}
                    override fun onSetFailure(p0: String?) {}
                }, sdp)
            }
            
            override fun onSetSuccess() {}
            override fun onCreateFailure(p0: String?) {}
            override fun onSetFailure(p0: String?) {}
        }, null)
    }
    
    override fun onSignalingChange(p0: PeerConnection.SignalingState?) {}
    override fun onIceConnectionChange(p0: PeerConnection.IceConnectionState?) {}
    override fun onIceGatheringChange(p0: PeerConnection.IceGatheringState?) {}
    override fun onIceCandidate(p0: org.webrtc.IceCandidate?) {}
    override fun onIceCandidatesRemoved(p0: Array<out org.webrtc.IceCandidate>?) {}
    override fun onAddStream(p0: org.webrtc.MediaStream?) {}
    override fun onRemoveStream(p0: org.webrtc.MediaStream?) {}
    override fun onDataChannel(p0: org.webrtc.DataChannel?) {}
    override fun onRenegotiationNeeded() {}
    override fun onAddTrack(p0: org.webrtc.RtpReceiver?, p1: Array<out org.webrtc.MediaStream>?) {}
}
