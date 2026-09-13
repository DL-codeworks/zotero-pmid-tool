param([string]$StartFolder)

Add-Type -AssemblyName System.Windows.Forms
$d = New-Object System.Windows.Forms.OpenFileDialog
$d.InitialDirectory = $StartFolder
$d.Filter = "Word documents (*.docx)|*.docx"
$d.Title = "Choose the original Word document"
$d.Multiselect = $false

if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    Write-Output $d.FileName
}
