// 全局变量
let mediaRecorder;
let audioChunks = [];
// API 地址策略：
// - 经 nginx 统一入口（默认/生产）：同源访问，/api、/ws 由 nginx 反代到网关
// - 8899 纯静态服务器（仅本地调试）：显式指向 8005 网关
const _servedByProxy = window.location.port !== '8899';
let apiUrl = _servedByProxy
    ? ''  // 同源相对路径
    : `http://${window.location.hostname || 'localhost'}:8005`;
let wsUrl = _servedByProxy
    ? `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}`
    : `ws://${window.location.hostname || 'localhost'}:8005`;
let sessionId = 'session_' + Math.floor(Math.random() * 1000000);
let voices = [];
let emotions = [];
let chatWs = null;  // WebSocket 连接
let audioQueue = [];  // 待播放音频队列
let isPlayingAudio = false;  // 是否正在播放

// ============================================
// 鉴权：令牌存储 + 请求自动携带 + 登录门禁
// ============================================
const AUTH_TOKEN_KEY = 'yuban_token';
let authRequired = false;  // 由 /health 的 auth_required 决定是否启用登录门禁

function getToken() {
    return localStorage.getItem(AUTH_TOKEN_KEY) || '';
}

function setToken(token) {
    if (token) {
        localStorage.setItem(AUTH_TOKEN_KEY, token);
    } else {
        localStorage.removeItem(AUTH_TOKEN_KEY);
    }
    updateLogoutVisibility();
}

function updateLogoutVisibility() {
    const btn = document.getElementById('logoutBtn');
    if (btn) btn.style.display = getToken() ? 'block' : 'none';
}

function showAuthOverlay(show, hintText) {
    const overlay = document.getElementById('authOverlay');
    if (!overlay) return;
    const wasHidden = overlay.style.display !== 'flex';
    overlay.style.display = show ? 'flex' : 'none';
    if (!show) return;
    if (hintText) {
        const hint = overlay.querySelector('.auth-hint');
        if (hint) hint.textContent = hintText;
    }
    // 仅在门禁首次弹出时抢焦点，避免打断正在输入的密码
    if (wasHidden) {
        const input = document.getElementById('authUsername');
        if (input) setTimeout(() => input.focus(), 60);
    }
}

// 全局 fetch 包装：自动注入 Authorization；收到 401 说明令牌失效 → 清令牌并回到登录页
const _nativeFetch = window.fetch.bind(window);
window.fetch = async function(input, init) {
    const opts = Object.assign({}, init);
    const headers = new Headers(opts.headers || {});
    const token = getToken();
    if (token && !headers.has('Authorization')) {
        headers.set('Authorization', 'Bearer ' + token);
    }
    opts.headers = headers;

    const resp = await _nativeFetch(input, opts);
    if (resp.status === 401 && authRequired) {
        setToken('');
        showAuthOverlay(true, '登录状态已失效，请重新登录');
    }
    return resp;
};

// 页面加载完成后执行
document.addEventListener('DOMContentLoaded', function() {
    // 初始化设置
    document.getElementById('apiUrl').value = apiUrl;
    document.getElementById('sessionId').value = sessionId;

    // 鉴权门禁（登录/退出）+ 网关连通性自检：先绑定事件，避免门禁弹出后按钮尚未就绪
    initAuth();

    // 标签页切换
    setupTabSwitching();
    
    // 语音录制（已改为文字输入 + WebSocket 实时对话）
    setupChatInput();
    
    // 语音录入
    setupVoiceInput();
    
    // 语音定义
    setupVoiceDefine();
    
    // 情感配置
    setupEmotionConfig();

    // 语音合成
    setupTts();

    // 角色管理
    setupCharacters();

    // 设置保存
    setupSettings();
    
    // 加载已保存的数据
    loadVoices();
    loadEmotions();
});

// 标签页切换（底部导航栏）
function setupTabSwitching() {
    const navBtns = document.querySelectorAll('.nav-btn');
    const tabContents = document.querySelectorAll('.tab-content');
    
    navBtns.forEach(btn => {
        btn.addEventListener('click', function() {
            const tabId = this.getAttribute('data-tab');
            
            // 移除所有活动状态
            navBtns.forEach(b => b.classList.remove('active'));
            tabContents.forEach(c => c.classList.remove('active'));
            
            // 添加当前活动状态
            this.classList.add('active');
            document.getElementById(tabId).classList.add('active');
            
            // 滚动到顶部
            const main = document.querySelector('.app-main');
            if (main) main.scrollTop = 0;
            
            // 如果切换到语音定义标签，更新基础语音下拉列表
            if (tabId === 'voice-define') {
                updateBaseVoiceSelect();
            }

            // 如果切换到语音合成标签，更新语音选择列表
            if (tabId === 'tts') {
                updateTtsVoiceSelect();
            }
        });
    });
}

// 对话交互：麦克风语音输入（主） + 文字输入（辅） + WebSocket 实时回复
function setupChatInput() {
    const chatInput = document.getElementById('chatInput');
    const chatSendBtn = document.getElementById('chatSendBtn');
    const micBtn = document.getElementById('micBtn');

    chatInput.addEventListener('keydown', function(e) {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendChatMessage();
        }
    });
    chatSendBtn.addEventListener('click', sendChatMessage);
    micBtn.addEventListener('click', toggleRecording);
}

// 输入锁定/解锁（等待回复期间禁用发送，文本回复完成即解锁）
function setInputLocked(locked) {
    const chatInput = document.getElementById('chatInput');
    const chatSendBtn = document.getElementById('chatSendBtn');
    const micBtn = document.getElementById('micBtn');
    if (chatInput) chatInput.disabled = locked;
    if (chatSendBtn) chatSendBtn.disabled = locked;
    if (micBtn) micBtn.disabled = locked;
    if (!locked) chatInput?.focus({preventScroll: true});
}

// 文字发送按钮
async function sendChatMessage() {
    const chatInput = document.getElementById('chatInput');
    const text = chatInput.value.trim();
    if (!text) return;
    chatInput.value = '';
    await sendUserText(text);
}

// 发送一条用户消息（文字或语音转写结果共用）
async function sendUserText(text) {
    addMessage('user', text);
    setInputLocked(true);

    if (!chatWs || chatWs.readyState !== WebSocket.OPEN) {
        try {
            await connectChatWebSocket();
        } catch (e) {
            addMessage('ai', '连接服务失败：' + e.message);
            setInputLocked(false);
            return;
        }
    }
    chatWs.send(JSON.stringify({ text: text }));
}

// ===== 麦克风录音 =====
let recState = { recording: false, stream: null, ctx: null, proc: null, chunks: [] };

async function toggleRecording() {
    if (recState.recording) {
        stopRecordingAndSend();
    } else {
        await startRecording();
    }
}

function showVoiceTip(msg, isError) {
    const tip = document.getElementById('voiceTip');
    tip.style.display = 'block';
    tip.textContent = msg;
    tip.className = 'chat-voice-tip' + (isError ? ' error' : '');
}
function hideVoiceTip() {
    document.getElementById('voiceTip').style.display = 'none';
}

async function startRecording() {
    // 环境预检
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        showVoiceTip('当前浏览器不支持录音。请用系统 Chrome/Safari 浏览器打开（微信内浏览器限制麦克风），且需要 HTTPS 环境。', true);
        return;
    }
    if (!window.isSecureContext) {
        showVoiceTip('当前是 HTTP 环境，浏览器禁止麦克风。请用 Chrome 打开后在 chrome://flags 开启不安全源信任，或部署 HTTPS。', true);
        return;
    }
    try {
        const stream = await navigator.mediaDevices.getUserMedia({
            audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true }
        });
        recState.stream = stream;
        const Ctx = window.AudioContext || window.webkitAudioContext;
        const ctx = new Ctx();
        recState.ctx = ctx;
        const source = ctx.createMediaStreamSource(stream);
        const proc = ctx.createScriptProcessor(4096, 1, 1);
        recState.proc = proc;
        recState.chunks = [];
        proc.onaudioprocess = (e) => {
            const data = e.inputBuffer.getChannelData(0);
            recState.chunks.push(new Float32Array(data));
        };
        source.connect(proc);
        proc.connect(ctx.destination);
        recState.recording = true;
        document.getElementById('micBtn').classList.add('recording');
        document.getElementById('recordingBar').style.display = 'flex';
        hideVoiceTip();
    } catch (err) {
        console.error('getUserMedia failed:', err);
        let hint = '无法访问麦克风';
        if (err.name === 'NotAllowedError' || err.name === 'PermissionDeniedError') {
            hint = '麦克风权限被拒绝，请在浏览器/系统设置中允许麦克风权限';
        } else if (err.name === 'NotFoundError') {
            hint = '未检测到麦克风设备';
        } else {
            hint = '麦克风启动失败：' + err.message;
        }
        showVoiceTip(hint + '。微信内浏览器通常禁止录音，请用系统浏览器打开。', true);
        cleanupRecording();
    }
}

function stopRecordingAndSend() {
    const ctx = recState.ctx;
    const chunks = recState.chunks;
    const sampleRate = ctx ? ctx.sampleRate : 48000;
    cleanupRecording();
    document.getElementById('micBtn').classList.remove('recording');
    document.getElementById('recordingBar').style.display = 'none';

    if (!chunks.length) {
        showVoiceTip('没有录到声音，请再试一次', true);
        return;
    }
    // 合并 Float32 → 重采样 16kHz → Int16 → WAV
    const merged = mergeFloat32(chunks);
    const pcm16 = resampleTo16kPcm16(merged, sampleRate);
    if (pcm16.length < 16000 * 0.3) {  // 短于 0.3 秒
        showVoiceTip('录音太短了，请长按或多说一会儿', true);
        return;
    }
    const wavBlob = encodeWav(pcm16, 16000);
    uploadAndTranscribe(wavBlob);
}

function cleanupRecording() {
    recState.recording = false;
    try { recState.proc && recState.proc.disconnect(); } catch (e) {}
    try { recState.stream && recState.stream.getTracks().forEach(t => t.stop()); } catch (e) {}
    try { recState.ctx && recState.ctx.close(); } catch (e) {}
    recState.proc = null; recState.stream = null; recState.ctx = null; recState.chunks = [];
}

function mergeFloat32(chunks) {
    const total = chunks.reduce((n, c) => n + c.length, 0);
    const out = new Float32Array(total);
    let off = 0;
    chunks.forEach(c => { out.set(c, off); off += c.length; });
    return out;
}

// 线性重采样到 16kHz 并转 Int16 PCM
function resampleTo16kPcm16(input, inRate) {
    const outRate = 16000;
    if (inRate === outRate) {
        return floatTo16(input);
    }
    const ratio = inRate / outRate;
    const outLen = Math.floor(input.length / ratio);
    const out = new Int16Array(outLen);
    for (let i = 0; i < outLen; i++) {
        const pos = i * ratio;
        const i0 = Math.floor(pos);
        const frac = pos - i0;
        const a = input[i0] || 0;
        const b = input[i0 + 1] || a;
        let s = a + (b - a) * frac;
        s = Math.max(-1, Math.min(1, s));
        out[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
    }
    return out;
}

function floatTo16(f32) {
    const out = new Int16Array(f32.length);
    for (let i = 0; i < f32.length; i++) {
        const s = Math.max(-1, Math.min(1, f32[i]));
        out[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
    }
    return out;
}

// 编码为 16kHz/16bit/单声道 WAV
function encodeWav(samples, sampleRate) {
    const buffer = new ArrayBuffer(44 + samples.length * 2);
    const view = new DataView(buffer);
    const writeStr = (off, s) => { for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i)); };
    writeStr(0, 'RIFF');
    view.setUint32(4, 36 + samples.length * 2, true);
    writeStr(8, 'WAVE'); writeStr(12, 'fmt ');
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);        // PCM
    view.setUint16(22, 1, true);        // 单声道
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    writeStr(36, 'data');
    view.setUint32(40, samples.length * 2, true);
    let off = 44;
    for (let i = 0; i < samples.length; i++, off += 2) view.setInt16(off, samples[i], true);
    return new Blob([view], { type: 'audio/wav' });
}

// 上传录音到转写接口，成功后直接进入对话流程
async function uploadAndTranscribe(wavBlob) {
    showVoiceTip('正在识别…');
    try {
        const fd = new FormData();
        fd.append('session_id', sessionId);
        fd.append('audio', wavBlob, 'recording.wav');
        const resp = await fetch(`${apiUrl}/api/voice/transcribe`, { method: 'POST', body: fd });
        const r = await resp.json();
        if (r.error) {
            showVoiceTip('识别失败：' + r.error, true);
            return;
        }
        if (!r.text) {
            showVoiceTip(r.warning || '没听清，请靠近麦克风再说一次', true);
            return;
        }
        hideVoiceTip();
        await sendUserText(r.text);
    } catch (e) {
        showVoiceTip('网络错误：' + e.message, true);
    }
}

// 建立 WebSocket 连接
function connectChatWebSocket() {
    return new Promise((resolve, reject) => {
        // WebSocket 无法自定义 Header，强制鉴权时通过 ?token= 传递
        const token = getToken();
        const url = `${wsUrl}/ws/chat/${sessionId}${token ? '?token=' + encodeURIComponent(token) : ''}`;
        // 日志避免带出令牌
        console.log('Connecting WebSocket:', `${wsUrl}/ws/chat/${sessionId}`);
        chatWs = new WebSocket(url);

        chatWs.onopen = () => {
            console.log('WebSocket connected');
            resolve();
        };

        chatWs.onerror = (e) => {
            console.error('WebSocket error:', e);
            reject(new Error('WebSocket 连接失败'));
        };

        chatWs.onmessage = async (event) => {
            // 二进制 = 音频
            if (event.data instanceof Blob) {
                const audioBlob = await event.data.arrayBuffer();
                const audioCtx = new AudioContext();
                const audioBuffer = await audioCtx.decodeAudioData(audioBlob);
                audioQueue.push(audioBuffer);
                playAudioQueue();
            }
            // 文本 = JSON 消息
            else if (typeof event.data === 'string') {
                try {
                    const msg = JSON.parse(event.data);
                    handleWsMessage(msg);
                } catch (e) {
                    console.log('Non-JSON WS message:', event.data);
                }
            }
        };

        chatWs.onclose = (event) => {
            console.log('WebSocket closed', event ? event.code : '');
            chatWs = null;
            setInputLocked(false);
            // 服务端因鉴权失败关闭（1008）→ 回登录页
            if (authRequired && event && event.code === 1008) {
                setToken('');
                showAuthOverlay(true, '登录状态已失效，请重新登录');
            }
        };
    });
}

// 处理 WebSocket JSON 消息
function handleWsMessage(msg) {
    switch (msg.type) {
        case 'status':
            if (msg.status === 'generating') {
                const who = msg.character ? `「${msg.character}」` : '语伴';
                // 思考占位：标记 streaming + thinking，首个文本到达时替换
                addMessage('ai', `${who}正在思考…`, true, true);
            } else if (msg.status === 'reply_done') {
                // 文本回复已完整 → 立即解锁输入（音频可能仍在后台合成播放）
                setInputLocked(false);
            } else if (msg.status === 'done') {
                // 全部音频推送完成
                console.log('reply fully done');
            }
            break;
        case 'text':
            // AI 回复文本（字幕）：首个 chunk 替换"正在思考"占位
            updateLastAiMessage(msg.content);
            break;
        case 'audio':
            console.log(`Audio ${msg.index}: sr=${msg.sample_rate}, size=${msg.size}`);
            break;
        case 'error':
            addMessage('ai', '错误：' + msg.message);
            setInputLocked(false);
            break;
    }
}

// 顺序播放音频队列
function playAudioQueue() {
    if (isPlayingAudio || audioQueue.length === 0) return;
    isPlayingAudio = true;
    const audioBuffer = audioQueue.shift();
    const audioCtx = new AudioContext();
    const source = audioCtx.createBufferSource();
    source.buffer = audioBuffer;
    source.connect(audioCtx.destination);
    source.onended = () => {
        isPlayingAudio = false;
        playAudioQueue();  // 播放下一段
    };
    source.start(0);
}

// 更新最后一条 AI 消息（首个文本替换"思考中"占位，之后追加）
function updateLastAiMessage(text) {
    const chatHistory = document.getElementById('chatHistory');
    let lastMsg = chatHistory.querySelector('.chat-message.ai:last-child .message-content');
    if (lastMsg && lastMsg.dataset.thinking === 'true') {
        // 首个回复文本：替换思考占位
        lastMsg.textContent = text;
        delete lastMsg.dataset.thinking;
        lastMsg.classList.remove('thinking-placeholder');
    } else if (lastMsg && lastMsg.dataset.streaming === 'true') {
        lastMsg.textContent += text;
    } else {
        addMessage('ai', text, true);
    }
    // 滚动到底部
    chatHistory.scrollTop = chatHistory.scrollHeight;
}

// 添加聊天消息
function addMessage(sender, text, streaming = false, thinking = false) {
    const chatHistory = document.getElementById('chatHistory');

    // 移除欢迎消息
    const welcome = chatHistory.querySelector('.chat-welcome');
    if (welcome) welcome.remove();

    const msgDiv = document.createElement('div');
    msgDiv.className = `chat-message ${sender}`;
    const content = document.createElement('div');
    content.className = 'message-content';
    content.textContent = text;
    if (streaming) content.dataset.streaming = 'true';
    if (thinking) {
        content.dataset.thinking = 'true';
        content.classList.add('thinking-placeholder');
    }
    msgDiv.appendChild(content);
    chatHistory.appendChild(msgDiv);
    chatHistory.scrollTop = chatHistory.scrollHeight;
}

// 语音录入
// ===== 录音音频：转 WAV + IndexedDB 存储 =====
// MediaRecorder 在浏览器中通常产出 webm/ogg，而网关只接受 wav/mp3/ogg/flac/m4a，
// 故录制后统一转为 WAV(PCM16) 再存储与上传；localStorage 存不下 Blob，故用 IndexedDB。

const VOICE_DB_NAME = 'yuban_voice_db';
const VOICE_DB_STORE = 'voice_audio';

function _openVoiceDB() {
    return new Promise((resolve, reject) => {
        const req = indexedDB.open(VOICE_DB_NAME, 1);
        req.onupgradeneeded = () => {
            const db = req.result;
            if (!db.objectStoreNames.contains(VOICE_DB_STORE)) {
                db.createObjectStore(VOICE_DB_STORE);
            }
        };
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
    });
}

async function saveVoiceAudio(id, blob) {
    const db = await _openVoiceDB();
    return new Promise((resolve, reject) => {
        const tx = db.transaction(VOICE_DB_STORE, 'readwrite');
        tx.objectStore(VOICE_DB_STORE).put(blob, id);
        tx.oncomplete = () => { db.close(); resolve(true); };
        tx.onerror = () => { db.close(); reject(tx.error); };
    });
}

async function getVoiceAudio(id) {
    const db = await _openVoiceDB();
    return new Promise((resolve, reject) => {
        const tx = db.transaction(VOICE_DB_STORE, 'readonly');
        const req = tx.objectStore(VOICE_DB_STORE).get(id);
        req.onsuccess = () => { db.close(); resolve(req.result || null); };
        req.onerror = () => { db.close(); reject(req.error); };
    });
}

async function deleteVoiceAudio(id) {
    const db = await _openVoiceDB();
    return new Promise((resolve) => {
        const tx = db.transaction(VOICE_DB_STORE, 'readwrite');
        tx.objectStore(VOICE_DB_STORE).delete(id);
        tx.oncomplete = () => { db.close(); resolve(true); };
        tx.onerror = () => { db.close(); resolve(false); };
    });
}

// 任意浏览器录音 Blob → WAV(PCM16)
async function _blobToWav(blob) {
    const arrayBuffer = await blob.arrayBuffer();
    const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    try {
        const audioBuffer = await audioCtx.decodeAudioData(arrayBuffer);
        return _encodeWav(audioBuffer);
    } finally {
        audioCtx.close();
    }
}

function _encodeWav(audioBuffer) {
    const numChannels = audioBuffer.numberOfChannels;
    const sampleRate = audioBuffer.sampleRate;
    const samples = audioBuffer.length;
    const blockAlign = numChannels * 2;          // PCM16
    const dataSize = samples * blockAlign;
    const buffer = new ArrayBuffer(44 + dataSize);
    const view = new DataView(buffer);

    const writeStr = (offset, str) => {
        for (let i = 0; i < str.length; i++) view.setUint8(offset + i, str.charCodeAt(i));
    };

    writeStr(0, 'RIFF');
    view.setUint32(4, 36 + dataSize, true);
    writeStr(8, 'WAVE');
    writeStr(12, 'fmt ');
    view.setUint32(16, 16, true);                // fmt chunk 长度
    view.setUint16(20, 1, true);                 // PCM 格式
    view.setUint16(22, numChannels, true);
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * blockAlign, true);
    view.setUint16(32, blockAlign, true);
    view.setUint16(34, 16, true);                // 位深
    writeStr(36, 'data');
    view.setUint32(40, dataSize, true);

    const channels = [];
    for (let c = 0; c < numChannels; c++) channels.push(audioBuffer.getChannelData(c));
    let offset = 44;
    for (let i = 0; i < samples; i++) {
        for (let c = 0; c < numChannels; c++) {
            const s = Math.max(-1, Math.min(1, channels[c][i]));
            view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
            offset += 2;
        }
    }
    return new Blob([view], { type: 'audio/wav' });
}

function setupVoiceInput() {
    const recordVoiceBtn = document.getElementById('recordVoiceBtn');
    const stopVoiceBtn = document.getElementById('stopVoiceBtn');
    const saveVoiceBtn = document.getElementById('saveVoiceBtn');
    
    let voiceRecorder;
    let voiceChunks = [];
    
    recordVoiceBtn.addEventListener('click', async function() {
        try {
            const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
            voiceRecorder = new MediaRecorder(stream);
            voiceChunks = [];
            
            voiceRecorder.ondataavailable = event => {
                if (event.data.size > 0) {
                    voiceChunks.push(event.data);
                }
            };
            
            voiceRecorder.onstop = () => {
                document.getElementById('recordVoiceBtn').style.display = 'flex';
                document.getElementById('stopVoiceBtn').style.display = 'none';
                document.getElementById('saveVoiceBtn').style.display = 'block';
            };
            
            voiceRecorder.start();
            document.getElementById('recordVoiceBtn').style.display = 'none';
            document.getElementById('stopVoiceBtn').style.display = 'flex';
            
            const responseDiv = document.getElementById('voiceInputResponse');
            responseDiv.innerHTML = '<p style="color: blue;">正在录制...</p>';
        } catch (error) {
            console.error('Error accessing microphone:', error);
            const responseDiv = document.getElementById('voiceInputResponse');
            responseDiv.innerHTML = '<p style="color: red;">无法访问麦克风，请检查权限设置。</p>';
        }
    });
    
    stopVoiceBtn.addEventListener('click', function() {
        if (voiceRecorder && voiceRecorder.state === 'recording') {
            voiceRecorder.stop();
            // 停止所有音频轨道
            voiceRecorder.stream.getTracks().forEach(track => track.stop());
        }
    });
    
    saveVoiceBtn.addEventListener('click', async function() {
        const voiceName = document.getElementById('voiceName').value;
        const voiceDescription = document.getElementById('voiceDescriptionInput').value;
        const responseDiv = document.getElementById('voiceInputResponse');

        if (!voiceName) {
            alert('请输入语音名称');
            return;
        }

        const voice = {
            id: 'voice_' + Date.now(),
            name: voiceName,
            description: voiceDescription,
            timestamp: new Date().toISOString()
        };

        // 录音数据转 WAV 后存入 IndexedDB（供"使用"时做声音克隆）
        let hasAudio = false;
        if (voiceChunks.length > 0) {
            try {
                const rawBlob = new Blob(voiceChunks, { type: voiceChunks[0].type || 'audio/webm' });
                const wavBlob = await _blobToWav(rawBlob);
                await saveVoiceAudio(voice.id, wavBlob);
                voice.hasAudio = true;
                hasAudio = true;
            } catch (e) {
                console.error('录音数据处理失败:', e);
            }
        }

        voices.push(voice);
        localStorage.setItem('voices', JSON.stringify(voices));

        responseDiv.innerHTML = hasAudio
            ? '<p style="color: green;">语音保存成功（含录音数据）！</p>'
            : '<p style="color: orange;">已保存，但未检测到录音数据（请先录制再保存）。</p>';

        // 重置表单
        document.getElementById('voiceName').value = '';
        document.getElementById('voiceDescriptionInput').value = '';
        document.getElementById('saveVoiceBtn').style.display = 'none';

        // 更新已保存的语音列表
        displaySavedVoices();
    });
}

// 语音定义
function setupVoiceDefine() {
    // 初始化滑块值显示
    const sliders = ['pitch', 'speed', 'volume', 'emotionIntensity'];
    sliders.forEach(sliderId => {
        const slider = document.getElementById(sliderId);
        const valueDisplay = document.getElementById(sliderId + 'Value');
        
        if (slider && valueDisplay) {
            slider.addEventListener('input', function() {
                valueDisplay.textContent = this.value;
            });
        }
    });
    
    const defineVoiceBtn = document.getElementById('defineVoiceBtn');
    
    defineVoiceBtn.addEventListener('click', async function() {
        const voiceName = document.getElementById('voiceNameDefine').value;
        const voiceDescription = document.getElementById('voiceDescriptionDefine').value;
        const baseVoice = document.getElementById('baseVoice').value;
        const pitch = parseFloat(document.getElementById('pitch').value);
        const speed = parseFloat(document.getElementById('speed').value);
        const volume = parseFloat(document.getElementById('volume').value);
        const emotionIntensity = parseFloat(document.getElementById('emotionIntensity').value);
        
        if (!voiceName) {
            alert('请输入语音名称');
            return;
        }
        
        // 获取用户上传的音频文件
        const voiceFileInput = document.getElementById('voiceFileDefine');
        // 参考音频（下方统一转为 WAV 后上传）
        
        if (voiceFileInput.files.length === 0) {
            alert('请先上传参考音频文件（支持 wav / mp3 / m4a / ogg 等）');
            return;
        }

        // 网关已原生支持 wav/mp3/ogg/flac/m4a 五种格式（见 api-gateway/_AUDIO_MAGIC），
        // 无需前端预先解码转码，直接上传原文件即可，避免 _encodeWav 的同步 CPU 密集循环阻塞主线程
        const audioFile = voiceFileInput.files[0];
        
        try {
            // 构建表单数据
            const formData = new FormData();
            formData.append('session_id', sessionId);
            formData.append('description', voiceDescription || voiceName);
            formData.append('audio', audioFile);
            formData.append('pitch', pitch);
            formData.append('speed', speed);
            formData.append('volume', volume);
            formData.append('emotion_intensity', emotionIntensity);
            
            // 调用后台API
            const response = await fetch(`${apiUrl}/api/voice/define`, {
                method: 'POST',
                body: formData
            });
            
            if (!response.ok) {
                // 透出服务端的具体错误（session_id/描述/音频格式等），避免只看到状态码无原因
                let detail = `HTTP ${response.status}`;
                try {
                    const errBody = await response.json();
                    if (errBody && errBody.error) detail = errBody.error;
                } catch (_) { /* 响应非 JSON，保留状态码 */ }
                throw new Error(detail);
            }
            
            const result = await response.json();
            console.log('Voice define response:', result);

            // 云 TTS 模式下服务端以 200 返回 error（声音定义不可用），此处明确提示而非误报成功
            if (result && result.error) {
                document.getElementById('voiceDefineResponse').innerHTML =
                    `<p style="color: red;">语音定义失败: ${result.error}</p>`;
                return;
            }
            
            // 保存语音定义到本地存储
            const voice = {
                id: 'voice_' + Date.now(),
                name: voiceName,
                description: voiceDescription,
                baseVoice: baseVoice,
                params: {
                    pitch: pitch,
                    speed: speed,
                    volume: volume,
                    emotionIntensity: emotionIntensity
                },
                voiceDefinition: result.voice_definition,
                voiceParams: result.voice_params,
                timestamp: new Date().toISOString()
            };
            
            voices.push(voice);
            localStorage.setItem('voices', JSON.stringify(voices));
            
            // 显示保存成功消息
            const responseDiv = document.getElementById('voiceDefineResponse');
            responseDiv.innerHTML = `
                <h4>语音定义结果</h4>
                <p style="color: green;">语音定义成功！</p>
                <p>名称: ${voiceName}</p>
                <p>描述: ${voiceDescription}</p>
                <p>音调: ${pitch}</p>
                <p>语速: ${speed}</p>
                <p>音量: ${volume}</p>
                <p>情感强度: ${emotionIntensity}</p>
                <p>语音定义: ${result.voice_definition}</p>
            `;
            
            // 重置表单
            document.getElementById('voiceNameDefine').value = '';
            document.getElementById('voiceDescriptionDefine').value = '';
            document.getElementById('baseVoice').value = '';
            document.getElementById('voiceFileDefine').value = '';
            document.getElementById('pitch').value = 1;
            document.getElementById('speed').value = 1;
            document.getElementById('volume').value = 1;
            document.getElementById('emotionIntensity').value = 0.5;
            document.getElementById('pitchValue').textContent = '1.0';
            document.getElementById('speedValue').textContent = '1.0';
            document.getElementById('volumeValue').textContent = '1.0';
            document.getElementById('emotionIntensityValue').textContent = '0.5';
            
            // 更新已保存的语音列表
            displaySavedVoices();
        } catch (error) {
            console.error('Error defining voice:', error);
            const responseDiv = document.getElementById('voiceDefineResponse');
            responseDiv.innerHTML = `<p style="color: red;">定义语音时出错: ${error.message}</p>`;
        }
    });
}

// 情感配置
function setupEmotionConfig() {
    // 初始化情感滑块值显示
    const emotionIds = ['happiness', 'sadness', 'anger', 'fear', 'disgust', 'surprise'];
    emotionIds.forEach(emotionId => {
        const slider = document.getElementById(emotionId);
        const valueDisplay = document.getElementById(emotionId + 'Value');
        
        if (slider && valueDisplay) {
            slider.addEventListener('input', function() {
                valueDisplay.textContent = this.value;
            });
        }
    });
    
    const saveEmotionBtn = document.getElementById('saveEmotionBtn');
    
    saveEmotionBtn.addEventListener('click', async function() {
        const emotionProfileName = document.getElementById('emotionProfileName').value;
        const emotionDescription = document.getElementById('emotionDescription').value;
        const happiness = parseFloat(document.getElementById('happiness').value);
        const sadness = parseFloat(document.getElementById('sadness').value);
        const anger = parseFloat(document.getElementById('anger').value);
        const fear = parseFloat(document.getElementById('fear').value);
        const disgust = parseFloat(document.getElementById('disgust').value);
        const surprise = parseFloat(document.getElementById('surprise').value);
        
        if (!emotionProfileName) {
            alert('请输入情感配置名称');
            return;
        }
        
        try {
            // 构建表单数据
            const formData = new FormData();
            formData.append('session_id', sessionId);
            formData.append('emotion_name', emotionProfileName);
            formData.append('emotion_description', emotionDescription);
            formData.append('happiness', happiness);
            formData.append('sadness', sadness);
            formData.append('anger', anger);
            formData.append('fear', fear);
            formData.append('disgust', disgust);
            formData.append('surprise', surprise);
            
            // 调用后台API
            const response = await fetch(`${apiUrl}/api/emotion-config`, {
                method: 'POST',
                body: formData
            });
            
            if (!response.ok) {
                // 透出服务端的具体错误（session_id/描述/音频格式等），避免只看到状态码无原因
                let detail = `HTTP ${response.status}`;
                try {
                    const errBody = await response.json();
                    if (errBody && errBody.error) detail = errBody.error;
                } catch (_) { /* 响应非 JSON，保留状态码 */ }
                throw new Error(detail);
            }
            
            const result = await response.json();
            console.log('Emotion config response:', result);
            
            // 保存情感配置到本地存储
            const emotionProfile = {
                id: 'emotion_' + Date.now(),
                name: emotionProfileName,
                description: emotionDescription,
                emotions: {
                    happiness: happiness,
                    sadness: sadness,
                    anger: anger,
                    fear: fear,
                    disgust: disgust,
                    surprise: surprise
                },
                emotionConfig: result.emotion_config,
                timestamp: new Date().toISOString()
            };
            
            emotions.push(emotionProfile);
            localStorage.setItem('emotions', JSON.stringify(emotions));
            
            // 显示保存成功消息
            const responseDiv = document.getElementById('emotionConfigResponse');
            responseDiv.innerHTML = `
                <h4>情感配置结果</h4>
                <p style="color: green;">情感配置保存成功！</p>
                <p>名称: ${emotionProfileName}</p>
                <p>描述: ${emotionDescription}</p>
                <p>愉悦: ${happiness}</p>
                <p>悲伤: ${sadness}</p>
                <p>愤怒: ${anger}</p>
                <p>恐惧: ${fear}</p>
                <p>厌恶: ${disgust}</p>
                <p>惊讶: ${surprise}</p>
            `;
            
            // 重置表单
            document.getElementById('emotionProfileName').value = '';
            document.getElementById('emotionDescription').value = '';
            document.getElementById('happiness').value = 0.5;
            document.getElementById('sadness').value = 0.1;
            document.getElementById('anger').value = 0.1;
            document.getElementById('fear').value = 0.1;
            document.getElementById('disgust').value = 0.1;
            document.getElementById('surprise').value = 0.3;
            document.getElementById('happinessValue').textContent = '0.5';
            document.getElementById('sadnessValue').textContent = '0.1';
            document.getElementById('angerValue').textContent = '0.1';
            document.getElementById('fearValue').textContent = '0.1';
            document.getElementById('disgustValue').textContent = '0.1';
            document.getElementById('surpriseValue').textContent = '0.3';
            
            // 更新已保存的情感配置列表
            displaySavedEmotions();
        } catch (error) {
            console.error('Error saving emotion config:', error);
            const responseDiv = document.getElementById('emotionConfigResponse');
            responseDiv.innerHTML = `<p style="color: red;">保存情感配置时出错: ${error.message}</p>`;
        }
    });
}

// 设置保存
function setupSettings() {
    const saveSettingsBtn = document.getElementById('saveSettingsBtn');
    
    saveSettingsBtn.addEventListener('click', function() {
        apiUrl = document.getElementById('apiUrl').value;
        sessionId = document.getElementById('sessionId').value;
        
        // 保存到本地存储
        localStorage.setItem('apiUrl', apiUrl);
        localStorage.setItem('sessionId', sessionId);
        
        alert('设置已保存');
        // 网关地址可能已变化，重新探测鉴权开关
        checkAuthGate();
    });
}

// ===== 鉴权初始化 =====
// 绑定登录/退出事件，并按 /health 返回的 auth_required 决定是否弹出登录门禁
function initAuth() {
    const form = document.getElementById('authForm');
    if (form) form.addEventListener('submit', handleAuthSubmit);
    const logoutBtn = document.getElementById('logoutBtn');
    if (logoutBtn) logoutBtn.addEventListener('click', handleLogout);
    updateLogoutVisibility();
    return checkAuthGate();
}

// 查询网关鉴权开关：AUTH_REQUIRED=false 时不弹门禁，避免把无账号用户锁死
async function checkAuthGate() {
    try {
        const resp = await fetch(`${apiUrl}/health`, { method: 'GET' });
        if (!resp.ok) {
            console.error('API connection failed:', resp.status);
            return;
        }
        console.log('API connection successful');
        const info = await resp.json();
        authRequired = !!info.auth_required;
    } catch (error) {
        // 网关不可达时不锁死界面，交由各接口自行报错
        console.error('无法获取鉴权开关:', error.message);
        return;
    }
    if (authRequired && !getToken()) {
        showAuthOverlay(true, '请输入账号密码以继续');
    }
}

// 登录：校验账号口令并换取 JWT（成功后重载页面，让各模块按已鉴权状态初始化）
async function handleAuthSubmit(e) {
    e.preventDefault();
    const usernameEl = document.getElementById('authUsername');
    const passwordEl = document.getElementById('authPassword');
    const errEl = document.getElementById('authError');
    const btn = document.getElementById('authSubmitBtn');
    const username = usernameEl.value.trim();
    const password = passwordEl.value;

    errEl.textContent = '';
    if (!username || !password) {
        errEl.textContent = '请输入账号和密码';
        return;
    }

    btn.disabled = true;
    btn.textContent = '登录中…';
    try {
        // 登录不走全局包装，避免失败时反过来触发展示门禁
        const resp = await _nativeFetch(`${apiUrl}/api/auth/login`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username: username, password: password })
        });
        const result = await resp.json().catch(() => ({}));
        if (!resp.ok || !result.token) {
            errEl.textContent = result.error || `登录失败（HTTP ${resp.status}）`;
            passwordEl.value = '';
            return;
        }
        setToken(result.token);
        passwordEl.value = '';
        window.location.reload();
    } catch (err) {
        errEl.textContent = '网络错误：' + err.message;
    } finally {
        btn.disabled = false;
        btn.textContent = '登录';
    }
}

// 退出登录：清令牌、断开连接并回到登录页
function handleLogout() {
    setToken('');
    if (chatWs) {
        try { chatWs.close(); } catch (e) {}
        chatWs = null;
    }
    window.location.reload();
}

// 加载已保存的语音
function loadVoices() {
    const savedVoices = localStorage.getItem('voices');
    if (savedVoices) {
        voices = JSON.parse(savedVoices);
        displaySavedVoices();
    }
}

// 显示已保存的语音
function displaySavedVoices() {
    const savedVoicesDiv = document.getElementById('savedVoices');
    if (!savedVoicesDiv) return;
    
    savedVoicesDiv.innerHTML = '';
    
    if (voices.length === 0) {
        savedVoicesDiv.innerHTML = '<p>暂无保存的语音</p>';
        return;
    }
    
    voices.forEach(voice => {
        const voiceCard = document.createElement('div');
        voiceCard.className = 'voice-card';
        voiceCard.innerHTML = `
            <div>
                <h5>${voice.name}</h5>
                <p>${voice.description || '无描述'}</p>
                <p style="font-size: 0.8em; color: #999;">${new Date(voice.timestamp).toLocaleString()}</p>
            </div>
            <div>
                <button class="use-btn" onclick="useVoice('${voice.id}')">使用</button>
                <button class="delete-btn" onclick="deleteVoice('${voice.id}')">删除</button>
            </div>
        `;
        savedVoicesDiv.appendChild(voiceCard);
    });
}

// 加载已保存的情感配置
function loadEmotions() {
    const savedEmotions = localStorage.getItem('emotions');
    if (savedEmotions) {
        emotions = JSON.parse(savedEmotions);
        displaySavedEmotions();
    }
}

// 显示已保存的情感配置
function displaySavedEmotions() {
    const savedEmotionsDiv = document.getElementById('savedEmotions');
    if (!savedEmotionsDiv) return;
    
    savedEmotionsDiv.innerHTML = '';
    
    if (emotions.length === 0) {
        savedEmotionsDiv.innerHTML = '<p>暂无保存的情感配置</p>';
        return;
    }
    
    emotions.forEach(emotion => {
        const emotionCard = document.createElement('div');
        emotionCard.className = 'emotion-card';
        emotionCard.innerHTML = `
            <div>
                <h5>${emotion.name}</h5>
                <p>${emotion.description || '无描述'}</p>
                <p style="font-size: 0.8em; color: #999;">${new Date(emotion.timestamp).toLocaleString()}</p>
            </div>
            <div>
                <button class="use-btn" onclick="useEmotion('${emotion.id}')">使用</button>
                <button class="delete-btn" onclick="deleteEmotion('${emotion.id}')">删除</button>
            </div>
        `;
        savedEmotionsDiv.appendChild(emotionCard);
    });
}

// 更新基础语音下拉列表
function updateBaseVoiceSelect() {
    const baseVoiceSelect = document.getElementById('baseVoice');
    if (!baseVoiceSelect) return;
    
    // 清空现有选项
    baseVoiceSelect.innerHTML = '<option value="">选择已保存的语音</option>';
    
    // 添加已保存的语音
    voices.forEach(voice => {
        const option = document.createElement('option');
        option.value = voice.id;
        option.textContent = voice.name;
        baseVoiceSelect.appendChild(option);
    });
}

// 使用语音（优先用录音做声音克隆；克隆不可用则退化为绑定语音风格描述）
async function useVoice(voiceId) {
    const voice = voices.find(v => v.id === voiceId);
    if (!voice) return;

    const description = voice.description || voice.name;
    let audioBlob = null;
    try {
        audioBlob = await getVoiceAudio(voiceId);
    } catch (e) {
        console.warn('读取录音数据失败:', e);
    }

    // 有录音数据 → 走后端声音克隆（需 GPU/VoxCPM2）
    if (audioBlob) {
        try {
            const fd = new FormData();
            fd.append('session_id', sessionId);
            fd.append('description', description);
            fd.append('audio', new File([audioBlob], voiceId + '.wav', { type: 'audio/wav' }));
            fd.append('pitch', 1.0);
            fd.append('speed', 1.0);
            fd.append('volume', 1.0);
            fd.append('emotion_intensity', 0.5);

            const resp = await fetch(`${apiUrl}/api/voice/define`, { method: 'POST', body: fd });
            const result = await resp.json();
            if (result && result.status === 'ok') {
                alert(`已使用语音「${voice.name}」（声音克隆完成），后续对话将使用该音色。`);
                return;
            }
            // 云 TTS 模式等场景克隆不可用，继续走退化路径
            console.warn('声音克隆不可用，退化为语音风格绑定:', result);
        } catch (e) {
            console.warn('声音克隆调用失败，退化为语音风格绑定:', e);
        }
    }

    // 退化路径：仅按描述绑定语音风格
    try {
        const body = {
            description,
            ...(voice.voiceDefinition ? { voice_definition: voice.voiceDefinition } : {}),
            ...(voice.voiceParams ? { voice_params: voice.voiceParams } : {}),
        };
        const resp = await fetch(`${apiUrl}/api/ai/sessions/${sessionId}/voice-style`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        await resp.json();
        alert(
            audioBlob
                ? `已绑定语音「${voice.name}」。注意：当前为云 TTS 模式，音色克隆需 GPU 部署后生效。`
                : `已绑定语音「${voice.name}」（该语音无录音数据，仅按描述绑定）。`
        );
    } catch (err) {
        alert(`绑定语音失败：${err.message}`);
    }
}

// 删除语音
function deleteVoice(voiceId) {
    if (confirm('确定要删除这个语音吗？')) {
        voices = voices.filter(v => v.id !== voiceId);
        localStorage.setItem('voices', JSON.stringify(voices));
        deleteVoiceAudio(voiceId).catch(() => {});
        displaySavedVoices();
        updateBaseVoiceSelect();
    }
}

// 使用情感配置（绑定到当前会话）
async function useEmotion(emotionId) {
    const emotion = emotions.find(e => e.id === emotionId);
    if (!emotion) return;

    try {
        const fd = new FormData();
        fd.append('session_id', sessionId);
        fd.append('emotion_name', emotion.name);
        fd.append('emotion_description', emotion.description || '');
        const e = emotion.emotions || {};
        fd.append('happiness', e.happiness ?? 0.5);
        fd.append('sadness', e.sadness ?? 0.5);
        fd.append('anger', e.anger ?? 0.5);
        fd.append('fear', e.fear ?? 0.5);
        fd.append('disgust', e.disgust ?? 0.5);
        fd.append('surprise', e.surprise ?? 0.5);

        const resp = await fetch(`${apiUrl}/api/emotion-config`, {
            method: 'POST',
            body: fd,
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        await resp.json();
        alert(`已启用情感配置「${emotion.name}」，后续合成将带上该情感风格。`);
    } catch (err) {
        alert(`启用情感配置失败：${err.message}`);
    }
}

// 删除情感配置
function deleteEmotion(emotionId) {
    if (confirm('确定要删除这个情感配置吗？')) {
        emotions = emotions.filter(e => e.id !== emotionId);
        localStorage.setItem('emotions', JSON.stringify(emotions));
        displaySavedEmotions();
    }
}

// 更新语音合成页的语音选择下拉列表
function updateTtsVoiceSelect() {
    const ttsVoiceSelect = document.getElementById('ttsVoiceSelect');
    if (!ttsVoiceSelect) return;

    const currentValue = ttsVoiceSelect.value;
    ttsVoiceSelect.innerHTML = '<option value="">会话已定义的语音</option>';

    voices.forEach(voice => {
        const option = document.createElement('option');
        option.value = voice.id;
        option.textContent = voice.name;
        ttsVoiceSelect.appendChild(option);
    });

    // 恢复之前的选择
    if (currentValue) ttsVoiceSelect.value = currentValue;
}

// 语音合成
function setupTts() {
    const ttsBtn = document.getElementById('ttsBtn');
    const ttsResponse = document.getElementById('ttsResponse');

    ttsBtn.addEventListener('click', async function() {
        const fullText = document.getElementById('ttsText').value.trim();
        if (!fullText) {
            ttsResponse.innerHTML = '<p style="color: red;">请输入合成文本</p>';
            return;
        }

        // 风格优先级：自定义描述 > 已保存语音描述 > 会话默认
        const customDesc = document.getElementById('ttsDescription').value.trim();
        let description = customDesc;
        if (!description) {
            const voiceId = document.getElementById('ttsVoiceSelect').value;
            const voice = voices.find(v => v.id === voiceId);
            if (voice) description = voice.description || voice.name;
        }

        const quickMode = document.getElementById('ttsQuickMode').checked;

        // 按句号/问号/感叹号/分号分句，保留标点
        const sentences = fullText.match(/[^。！？.!?；;]+[。！？.!?；;]?/g) || [fullText];
        const filtered = sentences.map(s => s.trim()).filter(s => s.length > 0);

        if (filtered.length === 0) {
            ttsResponse.innerHTML = '<p style="color: red;">文本无法分句，请检查输入</p>';
            return;
        }

        // 禁用按钮
        ttsBtn.disabled = true;
        ttsBtn.textContent = '合成中...';
        const startTime = Date.now();
        const audioUrls = [];
        let currentAudioIdx = 0;

        // 更新进度显示
        function updateProgress(status, sentenceIdx) {
            const elapsed = Math.floor((Date.now() - startTime) / 1000);
            const idx = sentenceIdx !== undefined ? sentenceIdx + 1 : '?';
            const total = filtered.length;
            const playedCount = currentAudioIdx;
            ttsResponse.innerHTML = `
                <p>⏳ 正在合成第 ${idx}/${total} 句... 已等待 ${elapsed} 秒</p>
                ${playedCount > 0 ? `<p>✅ 已播放 ${playedCount} 句</p>` : ''}
                <p class="tts-hint">分句流式合成，每句生成后自动播放。${quickMode ? '快速模式已开启。' : ''}</p>
            `;
        }

        // 顺序播放音频队列
        function playQueue() {
            if (currentAudioIdx >= audioUrls.length) return;

            const url = audioUrls[currentAudioIdx];
            const audio = new Audio(url);
            audio.onended = function() {
                currentAudioIdx++;
                playQueue();
            };
            audio.play();

            const elapsed = Math.floor((Date.now() - startTime) / 1000);
            const idx = currentAudioIdx + 1;
            const total = filtered.length;
            ttsResponse.innerHTML = `
                <h4>正在播放第 ${idx}/${total} 句（总耗时 ${elapsed} 秒）</h4>
                <audio controls autoplay src="${url}" style="width: 100%;"></audio>
                <p class="tts-hint">播放完成后自动播放下一句</p>
            `;
        }

        // 逐句合成：边生成边播放
        let nextToPlay = 0;
        for (let i = 0; i < filtered.length; i++) {
            updateProgress('synthesizing', i);

            try {
                const formData = new FormData();
                formData.append('session_id', sessionId);
                formData.append('text', filtered[i]);
                formData.append('description', description || '');
                formData.append('quick_mode', quickMode ? 'true' : 'false');

                const response = await fetch(`${apiUrl}/api/voice/tts`, {
                    method: 'POST',
                    body: formData
                });

                const contentType = response.headers.get('content-type') || '';
                if (contentType.includes('audio/wav')) {
                    const audioBlob = await response.blob();
                    const audioUrl = URL.createObjectURL(audioBlob);
                    audioUrls.push(audioUrl);

                    // 如果当前没有在播放，立即播放这句
                    if (audioUrls.length - 1 === nextToPlay) {
                        nextToPlay = audioUrls.length;
                        playQueue();
                    }
                } else {
                    const result = await response.json();
                    ttsResponse.innerHTML = `<p style="color: red;">第 ${i + 1} 句合成失败: ${result.error || '未知错误'}</p>`;
                    ttsBtn.disabled = false;
                    ttsBtn.textContent = '开始合成';
                    return;
                }
            } catch (error) {
                console.error('Error synthesizing speech:', error);
                ttsResponse.innerHTML = `<p style="color: red;">第 ${i + 1} 句合成出错: ${error.message}</p>`;
                ttsBtn.disabled = false;
                ttsBtn.textContent = '开始合成';
                return;
            }
        }

        // 全部生成完毕，显示最终结果
        const totalElapsed = Math.floor((Date.now() - startTime) / 1000);
        ttsResponse.innerHTML = `
            <h4>全部合成完成（${filtered.length} 句，总耗时 ${totalElapsed} 秒）</h4>
            <audio controls src="${audioUrls[0]}" style="width: 100%;"></audio>
            <p>选择播放：</p>
            <div style="display: flex; flex-direction: column; gap: 4px;">
                ${audioUrls.map((url, idx) => `<button style="padding: 6px; margin: 2px 0; background: #4CAF50; color: white; border: none; border-radius: 4px;" onclick="new Audio('${url}').play()">第${idx + 1}句</button>`).join('')}
            </div>
        `;
        ttsBtn.disabled = false;
        ttsBtn.textContent = '开始合成';
    });
}

// 从本地存储加载设置
function loadSettings() {
    const savedApiUrl = localStorage.getItem('apiUrl');
    const savedSessionId = localStorage.getItem('sessionId');
    
    if (savedApiUrl) {
        apiUrl = savedApiUrl;
        document.getElementById('apiUrl').value = apiUrl;
    }
    
    if (savedSessionId) {
        sessionId = savedSessionId;
        document.getElementById('sessionId').value = sessionId;
    }
}

// 页面加载时从本地存储加载设置
loadSettings();

// ===== 角色管理 =====
let characterOptions = null;
let currentCharacterId = null;

function setupCharacters() {
    loadCharacterOptions();
    loadCharacters();
    loadSessionCharacter();
    document.getElementById('createCharacterBtn').addEventListener('click', createCharacter);
}

// 加载枚举选项并填充下拉框/标签
async function loadCharacterOptions() {
    // 鉴权启动前（无 token）跳过，待登录后由 handleAuthSubmit 重载页面再执行
    if (authRequired && !getToken()) return;
    try {
        const resp = await fetch(`${apiUrl}/api/characters/options`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const result = await resp.json();
        characterOptions = result.options;

        fillSelect('charGender', characterOptions.genders);
        fillSelect('charAgeGroup', characterOptions.age_groups);
        fillSelect('charRelation', characterOptions.relations);
        renderTagGroup('charVoiceTags', characterOptions.voice_tags, 'voice');
        renderTagGroup('charPersonalityTags', characterOptions.personality_tags, 'personality');

        // 选项就绪后重新渲染角色列表（中文标签映射需要 options）
        loadCharacters();
    } catch (e) {
        console.warn('加载角色选项失败:', e.message);
    }
}

function fillSelect(selectId, dict) {
    const sel = document.getElementById(selectId);
    sel.innerHTML = Object.entries(dict).map(([k, v]) => `<option value="${k}">${v}</option>`).join('');
}

function renderTagGroup(containerId, dict, groupKey) {
    const container = document.getElementById(containerId);
    container.innerHTML = Object.entries(dict).map(([k, v]) =>
        `<label style="display:inline-flex; align-items:center; gap:4px; padding:5px 10px; border:1px solid #ddd; border-radius:16px; font-size:0.85rem; cursor:pointer; background:#fff;">
            <input type="checkbox" value="${k}" data-group="${groupKey}" style="margin:0;">${v}
        </label>`
    ).join('');
}

function getSelectedTags(groupKey) {
    return Array.from(document.querySelectorAll(`input[data-group="${groupKey}"]:checked`)).map(cb => cb.value);
}

// 加载角色列表
async function loadCharacters() {
    if (authRequired && !getToken()) return;
    try {
        const resp = await fetch(`${apiUrl}/api/characters`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const result = await resp.json();
        const list = result.characters || [];
        renderCharacterList('presetCharacterList', list.filter(c => c.is_preset));
        renderCharacterList('customCharacterList', list.filter(c => !c.is_preset));
    } catch (e) {
        console.warn('加载角色列表失败:', e.message);
    }
}

function renderCharacterList(containerId, characters) {
    const container = document.getElementById(containerId);
    if (!characters.length) {
        if (containerId === 'customCharacterList') {
            container.innerHTML = '<p style="color:#999; font-size:0.9rem;">还没有自定义角色，在下方创建。</p>';
        } else {
            container.innerHTML = '<p style="color:#999;">暂无预设角色</p>';
        }
        return;
    }
    container.innerHTML = characters.map(ch => {
        const vp = ch.voice_profile || {};
        const pp = ch.persona_profile || {};
        const tags = (vp.voice_tags || []).map(t => characterOptions?.voice_tags?.[t] || t).join('、');
        const personality = (pp.personality_tags || []).map(t => characterOptions?.personality_tags?.[t] || t).join('、');
        const active = ch.id === currentCharacterId;
        return `
        <div style="border:1px solid ${active ? '#4CAF50' : '#ddd'}; border-radius:10px; padding:12px; background:${active ? '#f1f8f2' : '#fff'};">
            <div style="display:flex; justify-content:space-between; align-items:center;">
                <strong>${ch.name} ${active ? '<span style="color:#4CAF50; font-size:0.8rem;">✓ 使用中</span>' : ''}</strong>
                <button onclick="bindCharacter('${ch.id}')" style="padding:6px 14px; background:${active ? '#ccc' : '#4CAF50'}; color:#fff; border:none; border-radius:6px; cursor:pointer;">${active ? '已选择' : '选择'}</button>
            </div>
            <div style="font-size:0.85rem; color:#666; margin-top:6px;">
                ${characterOptions?.genders?.[vp.gender] || ''} · ${characterOptions?.age_groups?.[vp.age_group] || ''} · 音色：${tags || '默认'}
            </div>
            <div style="font-size:0.85rem; color:#666; margin-top:2px;">
                ${pp.identity ? '身份：' + pp.identity + ' · ' : ''}关系：${characterOptions?.relations?.[pp.relation] || '朋友'}${personality ? ' · ' + personality : ''}
            </div>
        </div>`;
    }).join('');
}

// 绑定角色到当前会话
async function bindCharacter(characterId) {
    const formData = new FormData();
    formData.append('character_id', characterId);
    try {
        const resp = await fetch(`${apiUrl}/api/sessions/${sessionId}/character`, {
            method: 'POST', body: formData
        });
        const result = await resp.json();
        if (result.error) {
            alert('绑定失败：' + result.error);
            return;
        }
        currentCharacterId = characterId;
        showCharacterStatus(`已选择角色「${result.character_name}」，对话和合成将使用该角色的声音与人设。声音风格：${result.voice_prompt}`);
        loadCharacters();
        loadSessionCharacter();
    } catch (e) {
        alert('绑定失败：' + e.message);
    }
}

function showCharacterStatus(msg) {
    const el = document.getElementById('characterStatus');
    el.style.display = 'block';
    el.textContent = msg;
}

// 查询当前会话已绑定的角色
async function loadSessionCharacter() {
    if (authRequired && !getToken()) return;
    try {
        const resp = await fetch(`${apiUrl}/api/sessions/${sessionId}/character`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const result = await resp.json();
        if (result.character) {
            currentCharacterId = result.character.id;
            showCharacterStatus(`当前使用角色：「${result.character.name}」`);
            loadCharacters();
        }
        // 会话未绑定时 character=null，属正常业务状态，静默返回
    } catch (e) {
        console.warn('查询会话角色失败:', e.message);
    }
}

// 创建自定义角色
async function createCharacter() {
    const name = document.getElementById('charName').value.trim();
    if (!name) {
        alert('请填写角色名称');
        return;
    }
    const payload = {
        name: name,
        voice_profile: {
            gender: document.getElementById('charGender').value,
            age_group: document.getElementById('charAgeGroup').value,
            voice_tags: getSelectedTags('voice'),
            speed: 1.0
        },
        persona_profile: {
            identity: document.getElementById('charIdentity').value.trim(),
            relation: document.getElementById('charRelation').value,
            personality_tags: getSelectedTags('personality'),
            speaking_style: document.getElementById('charStyle').value.trim(),
            free_description: document.getElementById('charFreeDesc').value.trim()
        }
    };

    const btn = document.getElementById('createCharacterBtn');
    btn.disabled = true;
    btn.textContent = '创建中...';
    try {
        const resp = await fetch(`${apiUrl}/api/characters`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const result = await resp.json();
        if (result.error) {
            alert('创建失败：' + result.error);
        } else {
            document.getElementById('characterResponse').innerHTML =
                `<p style="color:green;">角色「${result.character.name}」创建成功！可在上方列表点"选择"绑定到当前会话。</p>`;
            document.getElementById('charName').value = '';
            document.getElementById('charIdentity').value = '';
            document.getElementById('charStyle').value = '';
            document.getElementById('charFreeDesc').value = '';
            document.querySelectorAll('#charVoiceTags input:checked, #charPersonalityTags input:checked').forEach(cb => cb.checked = false);
            loadCharacters();
        }
    } catch (e) {
        alert('创建失败：' + e.message);
    } finally {
        btn.disabled = false;
        btn.textContent = '创建角色';
    }
}