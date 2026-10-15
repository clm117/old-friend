# Test voice define endpoint
$sessionId = "test123"
$description = "warm female voice"
$audioFile = "D:\test_audio.wav"
$url = "http://localhost:8004/api/voice/define"

# Create test audio file if not exists
if (!(Test-Path $audioFile)) {
    New-Item -ItemType File -Path $audioFile -Value ([byte[]](0..100))
}

# Create multipart/form-data request
$boundary = [System.Guid]::NewGuid().ToString()
$request = [System.Net.HttpWebRequest]::Create($url)
$request.Method = "POST"
$request.ContentType = "multipart/form-data; boundary=$boundary"

# Build request body
$body = New-Object System.IO.MemoryStream
$writer = New-Object System.IO.StreamWriter($body)

# Add session_id parameter
$writer.WriteLine('--' + $boundary)
$writer.WriteLine('Content-Disposition: form-data; name="session_id"')
$writer.WriteLine()
$writer.WriteLine($sessionId)

# Add description parameter
$writer.WriteLine('--' + $boundary)
$writer.WriteLine('Content-Disposition: form-data; name="description"')
$writer.WriteLine()
$writer.WriteLine($description)

# Add audio file
$writer.WriteLine('--' + $boundary)
$writer.WriteLine('Content-Disposition: form-data; name="audio"; filename="test_audio.wav"')
$writer.WriteLine('Content-Type: audio/wav')
$writer.WriteLine()
$writer.Flush()

# Write audio file content
$audioContent = [System.IO.File]::ReadAllBytes($audioFile)
$body.Write($audioContent, 0, $audioContent.Length)

# End boundary
$writer.WriteLine()
$writer.WriteLine('--' + $boundary + '--')
$writer.Flush()

# Set request body length
$request.ContentLength = $body.Length

# Send request
$requestStream = $request.GetRequestStream()
$body.Position = 0
$body.CopyTo($requestStream)
$requestStream.Close()

# Get response
try {
    $response = $request.GetResponse()
    $reader = New-Object System.IO.StreamReader($response.GetResponseStream())
    $responseText = $reader.ReadToEnd()
    Write-Host 'Response: ' $responseText
    $response.Close()
} catch {
    Write-Host 'Error: ' $_.Exception.Message
    if ($_.Exception.Response) {
        $errorReader = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
        $errorText = $errorReader.ReadToEnd()
        Write-Host 'Error Response: ' $errorText
    }
}