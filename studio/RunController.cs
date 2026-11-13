using System.Diagnostics;
using System.IO;
using System.Globalization;
namespace XauStudio;
public sealed class RunController(Authentication auth)
{
    Process? process;
    LocalBridge? bridge;
    XboxService? service;
    string? stop;
    Task observation = Task.CompletedTask;
    public bool Running => process is { HasExited: false };
    public event Action<string>? Output;
    public event Action<int>? Ended;
    public async Task Start(Account account, int delay, bool retry, bool randomTitleOrder)
    {
        if (Running) return;
        await Cleanup();
        Directory.CreateDirectory(Storage.Folder(account.Id));
        stop = Path.Combine(Storage.Folder(account.Id), "pause.flag"); File.Delete(stop);
        service = new(auth);
        if (File.Exists(Storage.State(account.Id)))
        {
            using var db = Storage.Open(Storage.State(account.Id));
            if (double.TryParse(Storage.Meta(db, "cooldown_until"), CultureInfo.InvariantCulture, out var until))
                service.SetCooldown(DateTimeOffset.FromUnixTimeSeconds((long)Math.Ceiling(until)));
        }
        bridge = new(service);
        var python = Path.Combine(Storage.Root, "python", "python.exe");
        if (!File.Exists(python)) python = "python.exe";
        var start = new ProcessStartInfo(python) { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true, WorkingDirectory = Storage.Root };
        foreach (var arg in new[] { "-u", Path.Combine(Storage.Root, "studio/worker.py"), "--state", Storage.State(account.Id), "--report", Path.Combine(Storage.Folder(account.Id), "results.csv"), "--verified-manifest", Storage.Manifest, "--base", bridge.Url, "--execute", "--health-wait", "0", "--parent-pid", Environment.ProcessId.ToString(), "--delay", delay.ToString() }) start.ArgumentList.Add(arg);
        if (retry) start.ArgumentList.Add("--retry-unresolved");
        start.Environment["XAU_STUDIO_KEY"] = bridge.Key;
        start.Environment["XAU_STUDIO_STOP"] = stop;
        start.Environment["XAU_STUDIO_ACCOUNT"] = account.Id;
        start.Environment["XAU_STUDIO_RANDOM_TITLES"] = randomTitleOrder ? "1" : "0";
        start.Environment["PYTHONIOENCODING"] = "utf-8";
        start.StandardOutputEncoding = System.Text.Encoding.UTF8; start.StandardErrorEncoding = System.Text.Encoding.UTF8;
        var current = new Process { StartInfo = start, EnableRaisingEvents = true };
        current.OutputDataReceived += (_, e) => { if (e.Data != null) Output?.Invoke(e.Data); };
        current.ErrorDataReceived += (_, e) => { if (e.Data != null) Output?.Invoke(e.Data); };
        process = current;
        try { current.Start(); current.BeginOutputReadLine(); current.BeginErrorReadLine(); }
        catch { process = null; current.Dispose(); await Cleanup(); throw; }
        observation = Observe(current);
    }
    async Task Observe(Process current)
    {
        await current.WaitForExitAsync();
        Ended?.Invoke(current.ExitCode);
    }
    public async Task Pause()
    {
        if (stop != null) File.WriteAllText(stop, "pause");
        if (bridge != null) { await bridge.DisposeAsync(); bridge = null; }
        if (process != null && !process.HasExited) await process.WaitForExitAsync();
        await observation;
        await Cleanup();
    }
    async Task Cleanup()
    {
        await observation;
        if (bridge != null) { await bridge.DisposeAsync(); bridge = null; }
        service?.Dispose(); service = null;
        process?.Dispose(); process = null;
    }
}
