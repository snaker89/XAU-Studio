$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
& dotnet run --project studio/XauStudio.csproj -c Release
if ($LASTEXITCODE -ne 0) { throw 'XAU Studio failed to start. Install the .NET 9 SDK and check the output above.' }
