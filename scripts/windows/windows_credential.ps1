function Get-ZhuoruiWindowsCredential {
    param([string]$UserName)
    # Native Windows dialog: credentials never enter command arguments or logs.
    if (-not ('ZhuoruiCredentialPrompt' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class ZhuoruiCredentialPrompt {
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
    public struct Info {
        public int size; public IntPtr parent;
        public string message; public string caption; public IntPtr banner;
    }
    [DllImport("credui.dll", CharSet=CharSet.Unicode)]
    public static extern int CredUIPromptForCredentialsW(ref Info info, string target,
        IntPtr reserved, int error, StringBuilder user, int userMax,
        StringBuilder password, int passwordMax, ref bool save, int flags);
}
'@
    }
    $Info = New-Object ZhuoruiCredentialPrompt+Info
    $Info.size = [Runtime.InteropServices.Marshal]::SizeOf($Info)
    $Info.caption = 'Zhuorui automatic recovery'
    $Info.message = 'Enter this Windows account password to run recovery after a reboot, before sign-in. Windows Task Scheduler stores it; Zhuorui does not save it.'
    $UserBuffer = New-Object Text.StringBuilder 513
    $null = $UserBuffer.Append($UserName)
    $PasswordBuffer = New-Object Text.StringBuilder 256
    $Save = $false
    # GENERIC_CREDENTIALS | DO_NOT_PERSIST | ALWAYS_SHOW_UI | KEEP_USERNAME
    $Result = [ZhuoruiCredentialPrompt]::CredUIPromptForCredentialsW([ref]$Info, 'Zhuorui watchdog',
        [IntPtr]::Zero, 0, $UserBuffer, 513, $PasswordBuffer, 256, [ref]$Save, 0x140082)
    try {
        if ($Result -eq 1223) { throw 'Windows credential entry was cancelled. No task was installed.' }
        if ($Result -ne 0) { throw "Windows credential prompt failed with code $Result." }
        $SecurePassword = New-Object Security.SecureString
        for ($Index=0; $Index -lt $PasswordBuffer.Length; $Index++) { $SecurePassword.AppendChar($PasswordBuffer[$Index]) }
        $SecurePassword.MakeReadOnly()
        return New-Object Management.Automation.PSCredential($UserBuffer.ToString(), $SecurePassword)
    } finally {
        for ($Index=0; $Index -lt $PasswordBuffer.Length; $Index++) { $PasswordBuffer[$Index] = [char]0 }
        $null = $PasswordBuffer.Clear()
    }
}
