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
            StatusTitle.Text = "GESPEICHERTER FORTSCHRITT";
            StatusDetail.Text = "Fortschritt geladen. Konto links auswählen, um sich anzumelden und fortzusetzen.";
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
            StatusTitle.Text = "WIRD PAUSIERT";
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
            if (code != 0) { StatusTitle.Text = "LAUF GESTOPPT"; StatusDetail.Text = code == 3 ? "Der bisherige XAU-Lauf ist noch aktiv. Dessen Terminal schließen und hier erneut starten." : "Der Lauf wurde beendet. Details stehen im Protokoll; Fortschritt bleibt gespeichert."; }
            else
            {
                StatusTitle.Text = "PAUSIERT / ABGESCHLOSSEN";
                StatusDetail.Text = "Verarbeitung beendet, Fortschritt gespeichert. Offene Titel lassen sich mit Start fortsetzen.";
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
        catch (OperationCanceledException) { StatusTitle.Text = "ANMELDUNG ABGEBROCHEN"; }
        catch (Exception ex)
        {
            StatusTitle.Text = "AKTION NICHT ABGESCHLOSSEN";
            StatusDetail.Text = ex is SessionException ? "Bitte das Konto über „Konto anmelden“ erneut anmelden." : $"{ex.GetType().Name}: Bitte Verbindung und Anmeldung prüfen. Details im Protokoll.";
            // Third-party exceptions can contain tokens/URLs: never log their raw messages.
            Append($"[{DateTime.Now:HH:mm:ss}] {ex.GetType().Name} · Aktion wurde sicher angehalten.");
        }
        finally { busy = false; Controls(); await Refresh(); }
    }
    async Task Activate(Account account)
    {
        await runner.Pause();
        var next = new Authentication();
        StatusTitle.Text = "ANMELDUNG"; StatusDetail.Text = "Gespeicherte Sitzung wird geprüft …";
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
            Append("Bisheriger Fortschritt gefunden. Übernahme beim Start, sobald der bisherige Lauf beendet ist.");
        preferences.AccountId = account.Id; Save();
        AccountsList.ItemsSource = Storage.Accounts();
        AccountName.Text = account.Name; AuthStatus.Text = auth.State;
        ViewSelect.SelectedIndex = 0; viewGeneration++;
        StatusTitle.Text = "BEREIT"; StatusDetail.Text = "Direkt angemeldet. Start setzt den gespeicherten Fortschritt fort.";
    }
    async void AddAccount(object sender, RoutedEventArgs e) => await Guard(async () =>
    {
        await runner.Pause();
        var next = new Authentication();
        StatusTitle.Text = "ANMELDUNG"; StatusDetail.Text = "Microsoft-Anmeldung im separaten Fenster abschließen.";
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
        AccountName.Text = "Noch nicht angemeldet"; AuthStatus.Text = auth.State;
        StatusTitle.Text = "SITZUNG ENTFERNT"; StatusDetail.Text = "Der Fortschritt bleibt erhalten und wird bei erneuter Anmeldung wiederverwendet.";
    });
    async Task Begin()
    {
        if (auth.Current == null) return;
        Save(); ViewSelect.SelectedIndex = 0;
        await auth.Authorization(default);
        await runner.Start(auth.Current, preferences.Delay, RetryCheck.IsChecked == true, preferences.RandomTitleOrder);
        RetryCheck.IsChecked = false;
        StatusTitle.Text = "VERARBEITUNG"; StatusDetail.Text = "Konto und Warteschlange werden geprüft …";
    }
    async void StartRun(object sender, RoutedEventArgs e) => await Guard(Begin);
    async void PauseRun(object sender, RoutedEventArgs e) => await Guard(async () =>
    {
        StatusTitle.Text = "WIRD PAUSIERT"; StatusDetail.Text = "Laufende Anfrage wird beendet. Fortschritt wird gesichert.";
        await runner.Pause(); StatusTitle.Text = "PAUSIERT"; StatusDetail.Text = "Fortschritt gespeichert. Mit Start fortsetzen.";
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
            RunCountLabel.Text = runner.Running && ViewSelect.SelectedIndex == 0 ? "NEU VOLLSTÄNDIG · LAUF" : "NEU VOLLSTÄNDIG · LETZTER LAUF";
            RunCompletedCount.Text = "—";
            if (snapshot.runStats != null)
            {
                using var stats = JsonDocument.Parse(snapshot.runStats);
                RunCompletedCount.Text = stats.RootElement.GetProperty("newly_completed").GetInt32().ToString("N0");
            }
            Progress.Value = rows.Count == 0 ? 0 : 100.0 * rows.Count(r => r.Status is "Verified" or "Unsupported" or "Failed" or "NeedsAttention") / rows.Count;
            Footer.Text = ViewSelect.SelectedIndex == 0 ? "Bestätigt = alle zurückgegebenen Erfolge erreicht. Nicht unterstützt: " + rows.Count(r => r.Status == "Unsupported").ToString("N0") : "Ansicht des bisherigen Laufs. Die Originaldaten werden hier nur gelesen.";
            AuthStatus.Text = auth.State;
            Countdown.Text = "";
            if (runner.Running && ViewSelect.SelectedIndex == 0 && snapshot.status != null)
            {
                using var document = JsonDocument.Parse(snapshot.status); var status = document.RootElement;
                var mode = status.GetProperty("mode").GetString();
                StatusTitle.Text = mode switch { "rate_limit" => "RATENLIMIT · AUTOMATISCHE PAUSE", "session" => "ANMELDUNG / SITZUNG", "processing" => "VERARBEITUNG", "waiting" => "WARTE AUF WIEDERHOLUNG", "restart_budget" => "NEUSTARTBUDGET ERSCHÖPFT", "completed" => "ABGESCHLOSSEN", "paused" => "PAUSIERT", _ => "VERBINDUNG WIRD GEPRÜFT" };
                StatusDetail.Text = mode == "rate_limit" ? "Die Server-Wartefrist wird eingehalten. Danach geht es automatisch weiter." : status.GetProperty("detail").GetString();
                var remaining = status.TryGetProperty("until", out var until) ? until.GetDouble() - DateTimeOffset.UtcNow.ToUnixTimeSeconds() : 0;
                if (remaining > 0) Countdown.Text = TimeSpan.FromSeconds(remaining).ToString(@"hh\:mm\:ss");
            }
        }
        catch (Exception ex) when (ex is Microsoft.Data.Sqlite.SqliteException or IOException or JsonException) { Footer.Text = "Gespeicherter Stand wird erneut geladen …"; }
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
        TableInfo.Text = $"{filtered.Count:N0} Titel angezeigt · Suche nach Titel oder ID";
    }
    void SearchChanged(object sender, RoutedEventArgs e) => Filter();
    async void ViewChanged(object sender, SelectionChangedEventArgs e) { if (ready) { viewGeneration++; await Refresh(); } }
    void TitleDetails(object sender, MouseButtonEventArgs e)
    {
        if (TitlesGrid.SelectedItem is not TitleRow row) return;
        MessageBox.Show(this, $"{row.Name}\nID: {row.Id}\nStatus: {row.Label}\nVersuche: {row.Attempts}\nErfolge in letzter Antwort: {row.Total}\n\n{row.Reason}", "Titeldetails", MessageBoxButton.OK, MessageBoxImage.Information);
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
        var dialog = new SaveFileDialog { Filter = "CSV-Datei|*.csv", FileName = "XAU-Studio-Ergebnisse.csv" };
        if (dialog.ShowDialog(this) == true) { try { Storage.Export(dialog.FileName, rows); Append("CSV-Ergebnisse exportiert."); } catch { StatusDetail.Text = "CSV konnte nicht gespeichert werden."; } }
    }
    async void ImportCsv(object sender, RoutedEventArgs e) => await Guard(async () =>
    {
        var dialog = new OpenFileDialog { Filter = "CSV-Datei|*.csv" };
        if (dialog.ShowDialog(this) != true || auth.Current == null) return;
        var added = await Task.Run(() => CatalogImport.Import(dialog.FileName, auth.Current.Id));
        Append($"{added} Titel für den nächsten Start ergänzt. Vorhandene Status bleiben erhalten.");
        ViewSelect.SelectedIndex = 0;
    });
    void OpenData(object sender, RoutedEventArgs e) { Directory.CreateDirectory(Storage.Data); Process.Start(new ProcessStartInfo(Storage.Data) { UseShellExecute = true }); }
}
