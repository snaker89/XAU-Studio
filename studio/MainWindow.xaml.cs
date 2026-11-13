using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Threading;
using Microsoft.Win32;
namespace XauStudio;

public partial class MainWindow : Window
{
    Authentication auth = new();
    RunController runner;
    Preferences preferences;
    List<TitleRow> rows = [];
    readonly DispatcherTimer timer = new() { Interval = TimeSpan.FromSeconds(2) };
    bool ready, busy, refreshing, closing, canClose;
    int viewGeneration;
    Account? displayedAccount;
    public MainWindow()
    {
        preferences = Storage.Read<Preferences>(Path.Combine(Storage.Data, "preferences.json")) ?? new();
        runner = CreateRunner();
        InitializeComponent();
        AccountsList.ItemsSource = Storage.Accounts();
        AutoResume.IsChecked = preferences.ResumeOnOpen; DelayInput.Text = preferences.Delay.ToString();
        RandomTitleOrder.IsChecked = preferences.RandomTitleOrder;
        displayedAccount = Storage.Accounts().FirstOrDefault(a => a.Id == preferences.AccountId);
        if (displayedAccount != null)
        {
            AccountName.Text = displayedAccount.Name;
            StatusTitle.Text = "SAVED PROGRESS";
            StatusDetail.Text = "Progress loaded. Select an account on the left to sign in and resume.";
        }
        ready = true; Controls();
        timer.Tick += async (_, _) => await Refresh(); timer.Start();
        Loaded += async (_, _) =>
        {
            await Refresh();
            var saved = Storage.Accounts().FirstOrDefault(a => a.Id == preferences.AccountId);
            if (saved != null && preferences.ResumeOnOpen)
                await Guard(async () => { await Activate(saved); await Begin(); });
        };
        Closing += async (_, e) =>
        {
            if (canClose) return;
            e.Cancel = true;
            if (closing || busy) return;
            closing = true; busy = true; Controls(); timer.Stop();
            StatusTitle.Text = "PAUSING";
            try { await runner.Pause(); } catch { }
            auth.Dispose(); canClose = true; _ = Dispatcher.BeginInvoke(Close);
        };
    }
    RunController CreateRunner()
    {
        var controller = new RunController(auth);
        controller.Output += text => Dispatcher.BeginInvoke(() =>
        {
            Append(text);
            if (text.StartsWith("[STUDIO]")) StatusDetail.Text = text[8..];
        });
        controller.Ended += code => Dispatcher.BeginInvoke(async () =>
        {
            Controls(); await Refresh();
            if (code != 0) { StatusTitle.Text = "RUN STOPPED"; StatusDetail.Text = code == 3 ? "The previous XAU run is still active. Close its terminal and start again here." : "The run has ended. See the log for details. Progress has been saved."; }
            else
            {
                StatusTitle.Text = "PAUSED / COMPLETED";
                StatusDetail.Text = "Processing ended and progress was saved. Select Start to resume pending titles.";
            }
        });
        return controller;
    }
    void Append(string text)
    {
        if (LogBox.Text.Length > 100000) LogBox.Text = LogBox.Text[^60000..];
        LogBox.AppendText(text + Environment.NewLine); LogBox.ScrollToEnd();
    }
    void Controls()
    {
        if (!ready) return;
        StartButton.IsEnabled = !busy && !runner.Running && auth.Current != null;
        PauseButton.IsEnabled = !busy && runner.Running;
        AddButton.IsEnabled = !busy;
        AccountsList.IsEnabled = !busy;
        ForgetButton.IsEnabled = !busy && auth.Current != null;
        ImportButton.IsEnabled = !busy && !runner.Running && auth.Current != null;
        AutoResume.IsEnabled = !busy;
    }
    async Task Guard(Func<Task> action)
    {
        if (busy) return;
        busy = true; Controls();
        try { await action(); }
        catch (OperationCanceledException) { StatusTitle.Text = "SIGN-IN CANCELED"; }
        catch (Exception ex)
        {
            StatusTitle.Text = "ACTION INCOMPLETE";
            StatusDetail.Text = ex is SessionException ? "Use Sign in to reconnect the account." : $"{ex.GetType().Name}: Check your connection and sign-in. See the log for details.";
            // Third-party exceptions can contain tokens/URLs: never log their raw messages.
            Append($"[{DateTime.Now:HH:mm:ss}] {ex.GetType().Name} · Action stopped safely.");
        }
        finally { busy = false; Controls(); await Refresh(); }
    }
    async Task Activate(Account account)
    {
        await runner.Pause();
        var next = new Authentication();
        StatusTitle.Text = "SIGN-IN"; StatusDetail.Text = "Checking saved session …";
        try { await next.Restore(account); }
        catch { next.Dispose(); throw; }
        auth.Dispose(); auth = next; runner = CreateRunner();
        Adopt();
    }
    void Adopt()
    {
        var account = auth.Current!;
        displayedAccount = account;
        if (!File.Exists(Storage.State(account.Id)) && Storage.Previous(account.Id) != null)
            Append("Previous progress found. It will be imported on startup once the previous run has ended.");
        preferences.AccountId = account.Id; Save();
        AccountsList.ItemsSource = Storage.Accounts();
        AccountName.Text = account.Name; AuthStatus.Text = auth.State;
        ViewSelect.SelectedIndex = 0; viewGeneration++;
        StatusTitle.Text = "READY"; StatusDetail.Text = "Signed in directly. Start resumes saved progress.";
    }
    async void AddAccount(object sender, RoutedEventArgs e) => await Guard(async () =>
    {
        await runner.Pause();
        var next = new Authentication();
        StatusTitle.Text = "SIGN-IN"; StatusDetail.Text = "Complete Microsoft sign-in in the separate window.";
        try { await next.Login(this); } catch { next.Dispose(); throw; }
        auth.Dispose(); auth = next; runner = CreateRunner(); Adopt();
    });
    async void AccountSelected(object sender, SelectionChangedEventArgs e)
    {
        if (!ready || busy || AccountsList.SelectedItem is not Account account) return;
        displayedAccount = account; viewGeneration++;
        await Guard(() => Activate(account));
    }
    async void ForgetAccount(object sender, RoutedEventArgs e) => await Guard(async () =>
    {
        await runner.Pause();
        if (auth.Current is { } account) Storage.Forget(account);
        auth.Dispose(); auth = new(); runner = CreateRunner();
        AccountsList.ItemsSource = Storage.Accounts(); preferences.AccountId = null; Save();
        displayedAccount = null;
        AccountName.Text = "Not signed in"; AuthStatus.Text = auth.State;
        StatusTitle.Text = "SESSION REMOVED"; StatusDetail.Text = "Progress is retained and will be reused when you sign in again.";
    });
    async Task Begin()
    {
        if (auth.Current == null) return;
        Save(); ViewSelect.SelectedIndex = 0;
        await auth.Authorization(default);
        await runner.Start(auth.Current, preferences.Delay, RetryCheck.IsChecked == true, preferences.RandomTitleOrder);
        RetryCheck.IsChecked = false;
        StatusTitle.Text = "PROCESSING"; StatusDetail.Text = "Checking account and queue …";
    }
    async void StartRun(object sender, RoutedEventArgs e) => await Guard(Begin);
    async void PauseRun(object sender, RoutedEventArgs e) => await Guard(async () =>
    {
        StatusTitle.Text = "PAUSING"; StatusDetail.Text = "Finishing the current request and saving progress.";
        await runner.Pause(); StatusTitle.Text = "PAUSED"; StatusDetail.Text = "Progress saved. Select Start to resume.";
    });
    string? ViewFile() => ViewSelect.SelectedIndex switch
    {
        1 => Path.Combine(Storage.Root, "outputs/automation/state.sqlite"),
        2 => Path.Combine(Storage.Root, "outputs/automation/verified-account/state.sqlite"),
        _ => (auth.Current ?? displayedAccount) is not { } account ? null : File.Exists(Storage.State(account.Id)) ? Storage.State(account.Id) : Storage.Previous(account.Id)
    };
    async Task Refresh()
    {
        if (!ready || refreshing || closing) return;
        refreshing = true; var generation = viewGeneration;
        try
        {
            var file = ViewFile();
            var snapshot = await Task.Run(() =>
            {
                var values = Storage.Rows(file); string? status = null; string? runStats = null;
                if (file != null && File.Exists(file)) { using var db = Storage.Open(file); status = Storage.Meta(db, "runtime_status"); runStats = Storage.Meta(db, "studio_run_stats"); }
                return (values, status, runStats);
            });
            if (generation != viewGeneration) return;
            if (!rows.SequenceEqual(snapshot.values)) { rows = snapshot.values; Filter(); }
            var verified = rows.Count(r => r.Status == "Verified");
            var errors = rows.Count(r => r.Status is "Failed" or "NeedsAttention");
            var open = rows.Count(r => r.Status is "Pending" or "Retry" or "InFlight");
            TotalCount.Text = rows.Count.ToString("N0"); VerifiedCount.Text = verified.ToString("N0"); OpenCount.Text = open.ToString("N0"); ErrorCount.Text = errors.ToString("N0");
            RunCountLabel.Text = runner.Running && ViewSelect.SelectedIndex == 0 ? "NEWLY COMPLETED · RUN" : "NEWLY COMPLETED · LAST RUN";
            RunCompletedCount.Text = "—";
            if (snapshot.runStats != null)
            {
                using var stats = JsonDocument.Parse(snapshot.runStats);
                RunCompletedCount.Text = stats.RootElement.GetProperty("newly_completed").GetInt32().ToString("N0");
            }
            Progress.Value = rows.Count == 0 ? 0 : 100.0 * rows.Count(r => r.Status is "Verified" or "Unsupported" or "Failed" or "NeedsAttention") / rows.Count;
            Footer.Text = ViewSelect.SelectedIndex == 0 ? "Verified = all returned achievements achieved. Unsupported: " + rows.Count(r => r.Status == "Unsupported").ToString("N0") : "Viewing the previous run. Original data is read only.";
            AuthStatus.Text = auth.State;
            Countdown.Text = "";
            if (runner.Running && ViewSelect.SelectedIndex == 0 && snapshot.status != null)
            {
                using var document = JsonDocument.Parse(snapshot.status); var status = document.RootElement;
                var mode = status.GetProperty("mode").GetString();
                StatusTitle.Text = mode switch { "rate_limit" => "RATE LIMIT · AUTOMATIC PAUSE", "session" => "SIGN-IN / SESSION", "processing" => "PROCESSING", "waiting" => "WAITING TO RETRY", "restart_budget" => "RESTART BUDGET EXHAUSTED", "completed" => "COMPLETED", "paused" => "PAUSED", _ => "CHECKING CONNECTION" };
                StatusDetail.Text = mode == "rate_limit" ? "Waiting for the server cooldown to expire. Processing will resume automatically." : status.GetProperty("detail").GetString();
                var remaining = status.TryGetProperty("until", out var until) ? until.GetDouble() - DateTimeOffset.UtcNow.ToUnixTimeSeconds() : 0;
                if (remaining > 0) Countdown.Text = TimeSpan.FromSeconds(remaining).ToString(@"hh\:mm\:ss");
            }
        }
        catch (Exception ex) when (ex is Microsoft.Data.Sqlite.SqliteException or IOException or JsonException) { Footer.Text = "Reloading saved progress …"; }
        finally { refreshing = false; }
    }
    void Filter()
    {
        if (!ready) return;
        var query = SearchBox.Text.Trim(); var mode = FilterSelect.SelectedIndex;
        var filtered = rows.Where(r => (r.Name.Contains(query, StringComparison.OrdinalIgnoreCase) || r.Id.Contains(query)) && (mode switch { 1 => r.Status == "Verified", 2 => r.Status is "Pending" or "Retry" or "InFlight", 3 => r.Status is "NeedsAttention" or "Failed", 4 => r.Status == "Unsupported", _ => true })).ToList();
        var selected = (TitlesGrid.SelectedItem as TitleRow)?.Id;
        TitlesGrid.ItemsSource = filtered;
        if (selected != null) TitlesGrid.SelectedItem = filtered.FirstOrDefault(r => r.Id == selected);
        TableInfo.Text = $"{filtered.Count:N0} titles shown · Search by title or ID";
    }
    void SearchChanged(object sender, RoutedEventArgs e) => Filter();
    async void ViewChanged(object sender, SelectionChangedEventArgs e) { if (ready) { viewGeneration++; await Refresh(); } }
    void TitleDetails(object sender, MouseButtonEventArgs e)
    {
        if (TitlesGrid.SelectedItem is not TitleRow row) return;
        MessageBox.Show(this, $"{row.Name}\nID: {row.Id}\nStatus: {row.Label}\nAttempts: {row.Attempts}\nAchievements in last response: {row.Total}\n\n{row.Reason}", "Title details", MessageBoxButton.OK, MessageBoxImage.Information);
    }
    void Save()
    {
        preferences.ResumeOnOpen = AutoResume.IsChecked == true;
        preferences.RandomTitleOrder = RandomTitleOrder.IsChecked == true;
        preferences.Delay = int.TryParse(DelayInput.Text, out var delay) ? Math.Clamp(delay, 3, 300) : 3;
        DelayInput.Text = preferences.Delay.ToString();
        Storage.Json(Path.Combine(Storage.Data, "preferences.json"), preferences);
    }
    void SavePreferences(object sender, RoutedEventArgs e) { if (ready) Save(); }
    void ExportCsv(object sender, RoutedEventArgs e)
    {
        var dialog = new SaveFileDialog { Filter = "CSV file|*.csv", FileName = "XAU-Studio-Results.csv" };
        if (dialog.ShowDialog(this) == true) { try { Storage.Export(dialog.FileName, rows); Append("CSV results exported."); } catch { StatusDetail.Text = "Could not save the CSV file."; } }
    }
    async void ImportCsv(object sender, RoutedEventArgs e) => await Guard(async () =>
    {
        var dialog = new OpenFileDialog { Filter = "CSV file|*.csv" };
        if (dialog.ShowDialog(this) != true || auth.Current == null) return;
        var added = await Task.Run(() => CatalogImport.Import(dialog.FileName, auth.Current.Id));
        Append($"{added} titles added for the next start. Existing statuses are preserved.");
        ViewSelect.SelectedIndex = 0;
    });
    void OpenData(object sender, RoutedEventArgs e) { Directory.CreateDirectory(Storage.Data); Process.Start(new ProcessStartInfo(Storage.Data) { UseShellExecute = true }); }
}
