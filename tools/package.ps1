param([string]$OutputDirectory)
$ErrorActionPreference = 'Stop'
$standRoot = Split-Path $PSScriptRoot -Parent
if (!$OutputDirectory) { $OutputDirectory = Join-Path $standRoot '.state\dist' }
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

$files = @('.gitattributes', '.gitignore', 'README.md', 'compose.yaml')
foreach ($folder in @('docker', 'data', 'tools')) {
    $files += Get-ChildItem -LiteralPath (Join-Path $standRoot $folder) -Recurse -File |
        ForEach-Object { $_.FullName.Substring($standRoot.Length + 1).Replace('\', '/') }
}
if (!(Test-Path -LiteralPath (Join-Path $standRoot 'data\manifest.json'))) {
    throw 'data/manifest.json is missing.'
}
$bundleRoot = [IO.Path]::GetFullPath((Join-Path $standRoot 'data')) + [IO.Path]::DirectorySeparatorChar
$bundle = Get-Content (Join-Path $bundleRoot 'manifest.json') -Raw | ConvertFrom-Json
foreach ($item in (@($bundle.database) + @($bundle.objects) + @($bundle.extra_files))) {
    $path = [IO.Path]::GetFullPath((Join-Path $bundleRoot $item.file))
    if (!$path.StartsWith($bundleRoot, [StringComparison]::OrdinalIgnoreCase) -or
        !(Test-Path -LiteralPath $path -PathType Leaf) -or
        (Get-Item -LiteralPath $path).Length -ne $item.bytes -or
        (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $item.sha256) {
        throw 'A data file is missing or damaged; no archive was created.'
    }
}
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$name = 'ace-friends-local-' + (Get-Date -Format yyyyMMdd-HHmmss) + '.zip'
$destination = Join-Path ([IO.Path]::GetFullPath($OutputDirectory)) $name
$stream = [IO.File]::Open($destination, [IO.FileMode]::CreateNew)
try {
    $archive = New-Object IO.Compression.ZipArchive($stream, [IO.Compression.ZipArchiveMode]::Create, $true)
    try {
        foreach ($relative in ($files | Sort-Object -Unique)) {
            if ($relative -match '(^|/)(\.private|\.state|\.sources|\.git)(/|$)') { throw 'Private path rejected.' }
            $file = Join-Path $standRoot $relative
            if (!(Test-Path -LiteralPath $file -PathType Leaf)) { throw "Missing package file: $relative" }
            [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive, $file,
                ('ace-friends-local/' + $relative.Replace('\', '/')), [IO.Compression.CompressionLevel]::Optimal) | Out-Null
        }
    }
    finally { $archive.Dispose() }
}
finally { $stream.Dispose() }
$digest = (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant()
[IO.File]::WriteAllText($destination + '.sha256', "$digest  $name" + [Environment]::NewLine)
Write-Host "Handoff archive: $destination"
Write-Host "SHA256: $digest"
