param([string]$Python = 'python.exe', [string]$Dotnet = 'dotnet')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
& $Python (Join-Path $PSScriptRoot 'package_portable.py')
if ($LASTEXITCODE -ne 0) { throw 'Payload packaging failed.' }
$payload = Join-Path $projectRoot 'dist/portable-payload.zip'
& $Dotnet publish (Join-Path $PSScriptRoot 'XauStudio.csproj') -c Release -r win-x64 --self-contained true -p:RuntimeFrameworkVersion=9.0.22 -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true -p:IncludeAllContentForSelfExtract=true -p:EnableCompressionInSingleFile=true -p:DebugType=None "-p:PathMap=$projectRoot=/_/XAU-Studio" "-p:PortablePayload=$payload" -o (Join-Path $projectRoot 'dist/publish') --nologo
if ($LASTEXITCODE -ne 0) { throw 'Portable build failed.' }
Copy-Item -LiteralPath (Join-Path $projectRoot 'dist/publish/XauStudio.exe') -Destination (Join-Path $projectRoot 'dist/XAU-Studio-1.0.0-win-x64.exe') -Force
