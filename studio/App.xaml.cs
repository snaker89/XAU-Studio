using System.Windows;
using System.IO;
namespace XauStudio;
public partial class App : Application
{
    private Mutex? instance;
    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        if (e.Args.Contains("--portable-check"))
        {
            try
            {
                var titles = Storage.Catalog();
                var start = new System.Diagnostics.ProcessStartInfo(Path.Combine(Storage.Root, "python", "python.exe")) { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true };
                start.ArgumentList.Add("-c"); start.ArgumentList.Add("import ssl,sqlite3,urllib.request,sys;sys.path.insert(0,'studio');sys.path.insert(0,'automation');import worker;print('Python and worker OK')"); start.WorkingDirectory = Storage.Root;
                using var check = System.Diagnostics.Process.Start(start)!; var output = check.StandardOutput.ReadToEnd(); var errors = check.StandardError.ReadToEnd(); check.WaitForExit();
                Directory.CreateDirectory(Storage.Data);
                File.WriteAllText(Path.Combine(Storage.Data, "portable-check.txt"), $"Portable: {PortableAssets.Enabled}\nTitles: {titles.Count}\nAccounts: {Storage.Accounts().Count}\nPython exit: {check.ExitCode}\n{output}{errors}");
                Shutdown(check.ExitCode);
            }
            catch (Exception ex) { Directory.CreateDirectory(PortableAssets.Data); File.WriteAllText(Path.Combine(PortableAssets.Data, "portable-check.txt"), ex.ToString()); Shutdown(1); }
            return;
        }
        instance = new Mutex(true, "Local\\XauStudio.Desktop", out var first);
        if (!first) { MessageBox.Show("XAU Studio ist bereits geöffnet."); Shutdown(); return; }
        if (e.Args.Contains("--self-test")) { SelfTests.Run(); Shutdown(); return; }
        try { new MainWindow().Show(); }
        catch (Exception ex) { MessageBox.Show("Start fehlgeschlagen: " + ex.GetType().Name); Shutdown(1); }
    }
    protected override void OnExit(ExitEventArgs e) { instance?.Dispose(); base.OnExit(e); }
}
