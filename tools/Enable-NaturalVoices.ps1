# Enable-NaturalVoices.ps1
# Copies Narrator-exclusive Natural HD voice tokens into the standard
# Speech_OneCore registry path so any WinRT app (including Samsara) can use them.
#
# Run once as Administrator. Safe to re-run — skips already-copied tokens.
# A Windows feature update may reset these; just re-run this script if voices disappear.
#
# Usage:
#   Right-click → Run as Administrator
#   OR: Start-Process powershell -Verb RunAs -ArgumentList "-File Enable-NaturalVoices.ps1"

#Requires -RunAsAdministrator

$ErrorActionPreference = "Stop"

$sourceBase = "HKLM:\SOFTWARE\Microsoft\Speech Server\v11.0\Voices\Tokens"
$targetBase = "HKLM:\SOFTWARE\Microsoft\Speech_OneCore\Voices\Tokens"

# Also check the package-installed voice location
$packageSource = "HKLM:\SOFTWARE\Microsoft\Speech Server\v11.0"

Write-Host "=== Samsara Natural Voice Enabler ===" -ForegroundColor Cyan
Write-Host "Copying Natural HD voices to standard Speech API path..."
Write-Host ""

$copied = 0
$skipped = 0
$failed = 0

# Find all Natural HD voice tokens
$sources = @()
$searchPaths = @(
    "HKLM:\SOFTWARE\Microsoft\Speech Server\v11.0\Voices\Tokens",
    "HKLM:\SOFTWARE\Microsoft\Speech Server\v11.0\Voices\TokenEnums\MSTTSEnumerant\Tokens"
)

foreach ($searchPath in $searchPaths) {
    if (Test-Path $searchPath) {
        $tokens = Get-ChildItem $searchPath -ErrorAction SilentlyContinue
        foreach ($token in $tokens) {
            $defaultVal = (Get-ItemProperty $token.PSPath -Name "(default)" -ErrorAction SilentlyContinue)."(default)"
            if ($defaultVal -and ($defaultVal -like "*Natural*" -or $defaultVal -like "*Neural*" -or $defaultVal -like "*HD*")) {
                $sources += $token
            }
        }
    }
}

if ($sources.Count -eq 0) {
    Write-Host "No Natural HD voices found in Speech Server registry." -ForegroundColor Yellow
    Write-Host "Checking AppX package voice tokens..." -ForegroundColor Yellow
    
    # Try to find voices from installed AppX packages
    $voicePackages = Get-AppxPackage | Where-Object { $_.Name -like "MicrosoftWindows.Voice.*" }
    foreach ($pkg in $voicePackages) {
        $tokensXml = Join-Path $pkg.InstallLocation "Tokens.xml"
        if (Test-Path $tokensXml) {
            Write-Host "Found package: $($pkg.Name)" -ForegroundColor Green
            Write-Host "Token file: $tokensXml" -ForegroundColor Gray
            Write-Host ""
            Write-Host "Note: AppX-packaged voices require the registry import approach." -ForegroundColor Yellow
            Write-Host "Attempting to register voice token from package..." -ForegroundColor Yellow
            
            [xml]$tokens = Get-Content $tokensXml -Encoding UTF8
            foreach ($token in $tokens.Tokens.Category.Token) {
                $tokenName = $token.name
                $tokenDisplay = ($token.String | Where-Object { $_.name -eq "" }).value
                $installDir = $pkg.InstallLocation + "\"
                
                $targetPath = "$targetBase\$tokenName"
                
                if (Test-Path $targetPath) {
                    Write-Host "  SKIP (already exists): $tokenDisplay" -ForegroundColor Gray
                    $skipped++
                    continue
                }
                
                try {
                    # Create the token key
                    New-Item -Path $targetPath -Force | Out-Null
                    Set-ItemProperty -Path $targetPath -Name "(default)" -Value $tokenDisplay
                    
                    # Copy string values
                    foreach ($str in $token.String) {
                        if ($str.name -ne "") {
                            Set-ItemProperty -Path $targetPath -Name $str.name -Value $str.value
                        }
                    }
                    
                    # Set Attributes subkey
                    $attrPath = "$targetPath\Attributes"
                    New-Item -Path $attrPath -Force | Out-Null
                    foreach ($attr in $token.Attribute) {
                        $val = $attr.value -replace '\[INSTALLDIR\]', $installDir
                        Set-ItemProperty -Path $attrPath -Name $attr.name -Value $val
                    }
                    
                    # Fix INSTALLDIR placeholders — only touch known voice path keys,
                    # not PowerShell metadata properties (PSPath, PSParentPath, etc.)
                    foreach ($propName in @('LangDataPath', 'VoicePath', 'DataPath')) {
                        try {
                            $propVal = (Get-ItemProperty -Path $targetPath -Name $propName -ErrorAction Stop).$propName
                            if ($propVal -like '*[INSTALLDIR]*') {
                                $fixed = $propVal -replace [regex]::Escape('[INSTALLDIR]'), $installDir
                                Set-ItemProperty -Path $targetPath -Name $propName -Value $fixed
                            }
                        } catch { }
                    }
                    
                    # Remove PowerShell metadata keys that get accidentally written
                    foreach ($junk in @('PSPath', 'PSParentPath', 'PSProvider', 'PSChildName')) {
                        Remove-ItemProperty -Path $targetPath -Name $junk -ErrorAction SilentlyContinue
                    }
                    
                    Write-Host "  COPIED: $tokenDisplay" -ForegroundColor Green
                    $copied++
                } catch {
                    Write-Host "  FAILED: $tokenDisplay — $_" -ForegroundColor Red
                    $failed++
                }
            }
        }
    }
} else {
    # Copy from Speech Server registry
    foreach ($token in $sources) {
        $tokenName = $token.PSChildName
        $defaultVal = (Get-ItemProperty $token.PSPath -Name "(default)" -ErrorAction SilentlyContinue)."(default)"
        $targetPath = "$targetBase\$tokenName"
        
        if (Test-Path $targetPath) {
            Write-Host "  SKIP (already exists): $defaultVal" -ForegroundColor Gray
            $skipped++
            continue
        }
        
        try {
            Copy-Item -Path $token.PSPath -Destination $targetPath -Recurse -Force
            Write-Host "  COPIED: $defaultVal" -ForegroundColor Green
            $copied++
        } catch {
            Write-Host "  FAILED: $defaultVal — $_" -ForegroundColor Red
            $failed++
        }
    }
}

Write-Host ""
Write-Host "=== Done ===" -ForegroundColor Cyan
Write-Host "  Copied:  $copied" -ForegroundColor Green
Write-Host "  Skipped: $skipped" -ForegroundColor Gray
Write-Host "  Failed:  $failed" -ForegroundColor Red
Write-Host ""

if ($copied -gt 0) {
    Write-Host "Restart Samsara to pick up the new voices." -ForegroundColor Yellow
    Write-Host "They will appear in Settings > TTS > Voice dropdown." -ForegroundColor Yellow
} elseif ($skipped -gt 0) {
    Write-Host "Voices already registered. If Samsara still doesn't show them," -ForegroundColor Yellow
    Write-Host "make sure Samsara is fully restarted (not just reloaded)." -ForegroundColor Yellow
}
