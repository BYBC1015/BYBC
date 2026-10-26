<#
Send or inspect Hub mail. Set HUB_MAIL_API_URL and HUB_MAIL_API_KEY in the
execution environment. Reuse EventId and unchanged content for one event.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('send', 'status')]
    [string]$Action,
    [string[]]$To,
    [string]$Subject,
    [string]$Text,
    [string]$TextFile,
    [string]$EventId,
    [string]$MessageId
)

$ErrorActionPreference = 'Stop'

function Stop-MailExample([string]$Message) {
    [Console]::Error.WriteLine($Message)
    exit 1
}

$mailEndpoint = ([string]$env:HUB_MAIL_API_URL).TrimEnd('/')
$mailKey = ([string]$env:HUB_MAIL_API_KEY).Trim()
$mailUri = $null
if (-not [Uri]::TryCreate($mailEndpoint, [UriKind]::Absolute, [ref]$mailUri) -or
    $mailUri.Scheme -ne 'https' -or $mailUri.UserInfo -or $mailUri.Query -or
    $mailUri.Fragment -or -not $mailUri.AbsolutePath.EndsWith('/api/v1/mail/messages')) {
    Stop-MailExample 'Set HUB_MAIL_API_URL to the HTTPS mail API endpoint.'
}
if ($mailKey -cnotmatch '^hmail_[A-Za-z0-9_-]{43}$') {
    Stop-MailExample 'Set HUB_MAIL_API_KEY to a valid application mail key.'
}

$mailHeaders = @{ Authorization = "Bearer $mailKey"; Accept = 'application/json' }
$mailRequest = @{
    Uri = $mailEndpoint
    Method = 'GET'
    Headers = $mailHeaders
    TimeoutSec = 30
    MaximumRedirection = 0
    UseBasicParsing = $true
    ErrorAction = 'Stop'
}

if ($Action -eq 'send') {
    if ($EventId -cnotmatch '^[\x21-\x7e]{8,128}$') {
        Stop-MailExample 'EventId must contain 8-128 printable ASCII characters without spaces.'
    }
    if (-not $To -or $To.Count -gt 20 -or -not $Subject) {
        Stop-MailExample 'Provide 1-20 recipients and a subject.'
    }
    if ($PSBoundParameters.ContainsKey('Text') -eq $PSBoundParameters.ContainsKey('TextFile')) {
        Stop-MailExample 'Provide exactly one of Text or TextFile.'
    }
    if ($TextFile) {
        try { $Text = [IO.File]::ReadAllText((Get-Item -LiteralPath $TextFile).FullName, [Text.Encoding]::UTF8) }
        catch { Stop-MailExample 'Cannot read the UTF-8 message file.' }
    }
    $mailPayload = [ordered]@{ to = @($To); subject = $Subject; text = $Text }
    $mailBody = [Text.Encoding]::UTF8.GetBytes(($mailPayload | ConvertTo-Json -Depth 5 -Compress))
    if ($mailBody.Length -gt 262144 -or [Text.Encoding]::UTF8.GetByteCount($Text) -gt 131072) {
        Stop-MailExample 'Message exceeds the API content limit.'
    }
    $mailHeaders['Idempotency-Key'] = $EventId
    $mailRequest.Method = 'POST'
    $mailRequest.ContentType = 'application/json; charset=utf-8'
    $mailRequest.Body = $mailBody
}
else {
    if ($MessageId -cnotmatch '^[A-Za-z0-9_-]{1,128}$') {
        Stop-MailExample 'Provide a valid MessageId.'
    }
    $mailRequest.Uri = "$mailEndpoint/$MessageId"
}

$mailResponse = $null
for ($mailAttempt = 0; $mailAttempt -lt 4; $mailAttempt++) {
    $mailDelay = [Math]::Min(30, [Math]::Pow(2, $mailAttempt))
    try {
        $mailResponse = Invoke-WebRequest @mailRequest
        break
    }
    catch {
        $mailStatus = 0
        try { $mailStatus = [int]$_.Exception.Response.StatusCode } catch { }
        $mailRetryable = $mailStatus -eq 0 -or $mailStatus -in @(408, 425, 429) -or
            ($mailStatus -ge 500 -and $mailStatus -le 599)
        if (-not $mailRetryable) {
            Stop-MailExample "Mail API HTTP $mailStatus. Check the key, message fields, and event ID."
        }
        if ($mailAttempt -eq 3) {
            Stop-MailExample "Mail API request failed (HTTP $mailStatus). Retry later with the same EventId and content."
        }
        try {
            $mailRetryAfter = [string]$_.Exception.Response.Headers['Retry-After']
            if ($mailRetryAfter -match '^\d+$') {
                $mailDelay = [Math]::Min(60, [Math]::Max($mailDelay, [double]$mailRetryAfter))
            }
        } catch { }
        Start-Sleep -Seconds $mailDelay
    }
}

try { $mailResult = $mailResponse.Content | ConvertFrom-Json }
catch { Stop-MailExample 'Mail API returned an invalid response.' }
if ($mailResult.id -cnotmatch '^[A-Za-z0-9_-]{1,128}$' -or $null -eq $mailResult.deliveries) {
    Stop-MailExample 'Mail API returned an invalid message record.'
}
$mailStates = @('queued', 'sending', 'retry_wait', 'accepted', 'delivered', 'delivery_delayed',
    'bounced', 'complained', 'suppressed', 'failed', 'uncertain', 'cancelled', 'expired')
$mailOutput = @()
foreach ($mailDelivery in $mailResult.deliveries) {
    if ($mailDelivery.id -cnotmatch '^[A-Za-z0-9_-]{1,128}$' -or $mailDelivery.status -notin $mailStates) {
        Stop-MailExample 'Mail API returned an invalid delivery record.'
    }
    $mailOutput += [ordered]@{ id = $mailDelivery.id; status = $mailDelivery.status }
}
[ordered]@{ id = $mailResult.id; deliveries = @($mailOutput) } | ConvertTo-Json -Depth 5 -Compress
