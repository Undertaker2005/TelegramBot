param(
    [switch]$IncludeNonCommercial
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$projectRoot = Split-Path -Parent $PSScriptRoot
$target = Join-Path (Join-Path $projectRoot 'data') 'english_cefr_datasets'
$downloads = Join-Path (Join-Path $projectRoot 'data') 'downloads'
New-Item -ItemType Directory -Force -Path $target | Out-Null
New-Item -ItemType Directory -Force -Path $downloads | Out-Null

function Save-Dataset([string]$Url, [string]$Name) {
    Invoke-WebRequest -Uri $Url -OutFile (Join-Path $target $Name)
    Write-Host "Downloaded $Name"
}

Save-Dataset `
    'https://raw.githubusercontent.com/openlanguageprofiles/olp-en-cefrj/master/cefrj-vocabulary-profile-1.5.csv' `
    'cefrj-vocabulary-profile-1.5.csv'
Save-Dataset `
    'https://raw.githubusercontent.com/openlanguageprofiles/olp-en-cefrj/master/cefrj-grammar-profile-20180315.csv' `
    'cefrj-grammar-profile-20180315.csv'
Save-Dataset `
    'https://raw.githubusercontent.com/openlanguageprofiles/olp-en-cefrj/master/octanove-vocabulary-profile-c1c2-1.0.csv' `
    'octanove-vocabulary-profile-c1c2-1.0.csv'

Invoke-WebRequest `
    -Uri 'https://en-word.net/static/english-wordnet-2025-json.zip' `
    -OutFile (Join-Path $downloads 'english-wordnet-2025-json.zip')
Invoke-WebRequest `
    -Uri 'https://raw.githubusercontent.com/globalwordnet/english-wordnet/main/LICENSE.md' `
    -OutFile (Join-Path $downloads 'LICENSE-OEWN.md')
Invoke-WebRequest `
    -Uri 'https://raw.githubusercontent.com/globalwordnet/english-wordnet/main/WNDB_License.txt' `
    -OutFile (Join-Path $downloads 'LICENSE-WordNet.txt')

if ($IncludeNonCommercial) {
    Invoke-WebRequest `
        -Uri 'https://huggingface.co/datasets/UniversalCEFR/cefr_sp_en/resolve/main/cefr-sp.json' `
        -OutFile (Join-Path $target 'cefr-sp.json')
    Write-Warning 'CEFR-SP is CC BY-NC-SA 4.0. Do not use it in a commercial bot.'
}
else {
    Write-Host 'Skipped CEFR-SP (CC BY-NC-SA 4.0). Use -IncludeNonCommercial only for non-commercial deployments.'
}

Write-Host "CEFR-J and Open English WordNet files saved under $target and $downloads."
Write-Host 'Build the searchable dataset with: .\.venv312\Scripts\python.exe scripts\build_open_dataset.py'
