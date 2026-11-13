using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Microsoft.Data.Sqlite;
namespace XauStudio;

public record Account(string Id, string Name) { public override string ToString() => Name; }
public record TitleRow(string Id, string Name, string Status, int Attempts = 0, string Reason = "", int Total = 0)
{
    public string Label => Status switch { "Verified" => "Verified", "Pending" => "Pending", "Retry" => "Retry", "InFlight" => "Processing", "Unsupported" => "Unsupported", "Failed" => "Excluded", "NeedsAttention" => "Needs attention", _ => Status };
}
public sealed class Preferences
{
    public string? AccountId { get; set; }
    public bool ResumeOnOpen { get; set; }
    public int Delay { get; set; } = 3;
    public bool RandomTitleOrder { get; set; }
}
public static class Storage
{
    public static string Root { get; } = FindRoot();
    public static string Data { get; } = InitializeData();
    static string InitializeData()
    {
        var target = PortableAssets.Data;
        Directory.CreateDirectory(target);
        var legacy = Path.Combine(Root, "outputs", "studio");
        if (PortableAssets.Enabled)
        {
            for (var dir = new DirectoryInfo(Path.GetDirectoryName(Environment.ProcessPath!)!); dir != null; dir = dir.Parent)
                if (File.Exists(Path.Combine(dir.FullName, "automation", "xau_runner.py")))
                { legacy = Path.Combine(dir.FullName, "outputs", "studio"); break; }
        }
        {
            if (Directory.Exists(legacy))
            {
                foreach (var file in new[] { "accounts.json", "preferences.json" })
                {
                    var source = Path.Combine(legacy, file); var destination = Path.Combine(target, file);
                    if (File.Exists(source) && !File.Exists(destination)) File.Copy(source, destination);
                }
                foreach (var folder in Directory.GetDirectories(legacy))
                {
                    var id = Path.GetFileName(folder);
                    if (id.Length == 0 || !id.All(char.IsAsciiDigit)) continue;
                    var destination = Path.Combine(target, id); Directory.CreateDirectory(destination);
                    var oldState = Path.Combine(folder, "state.sqlite"); var newState = Path.Combine(destination, "state.sqlite");
                    if (File.Exists(oldState) && !File.Exists(newState))
                    {
                        var staging = newState + ".migration";
                        using (var source = Open(oldState))
                        using (var copy = Open(staging, true)) source.BackupDatabase(copy);
                        File.Move(staging, newState);
                    }
                    foreach (var file in new[] { "session.bin", "additional-titles.json" })
                    {
                        var source = Path.Combine(folder, file); var dest = Path.Combine(destination, file);
                        if (File.Exists(source) && !File.Exists(dest)) File.Copy(source, dest);
                    }
                }
            }
        }
        return target;
    }
    public static string Manifest => Path.Combine(Root, "automation", "verified-titles-2026-11-06.json");
    static string FindRoot()
    {
        if (PortableAssets.Enabled) return PortableAssets.Extract();
        for (var dir = new DirectoryInfo(AppContext.BaseDirectory); dir != null; dir = dir.Parent)
            if (File.Exists(Path.Combine(dir.FullName, "automation", "xau_runner.py"))) return dir.FullName;
        throw new DirectoryNotFoundException("Project folder is missing.");
    }
    public static string Folder(string id)
    {
        if (id.Length == 0 || !id.All(char.IsAsciiDigit)) throw new InvalidDataException("Invalid account.");
        return Path.Combine(Data, id);
    }
    public static string State(string id) => Path.Combine(Folder(id), "state.sqlite");
    public static void Atomic(string file, byte[] content)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(file)!);
        var temp = file + ".tmp";
        File.WriteAllBytes(temp, content);
        File.Move(temp, file, true);
    }
    public static List<Account> Accounts() => Read<List<Account>>(Path.Combine(Data, "accounts.json")) ?? [];
    public static T? Read<T>(string file) => File.Exists(file) ? JsonSerializer.Deserialize<T>(File.ReadAllBytes(file)) : default;
    public static void Json<T>(string file, T value) => Atomic(file, JsonSerializer.SerializeToUtf8Bytes(value));
    public static void SaveAccount(Account account, string refresh)
    {
        Atomic(Path.Combine(Folder(account.Id), "session.bin"), ProtectedData.Protect(Encoding.UTF8.GetBytes(refresh), null, DataProtectionScope.CurrentUser));
        var accounts = Accounts(); accounts.RemoveAll(a => a.Id == account.Id); accounts.Add(account);
        Json(Path.Combine(Data, "accounts.json"), accounts);
    }
    public static string Token(string id) => Encoding.UTF8.GetString(ProtectedData.Unprotect(File.ReadAllBytes(Path.Combine(Folder(id), "session.bin")), null, DataProtectionScope.CurrentUser));
    public static void Forget(Account account)
    {
        File.Delete(Path.Combine(Folder(account.Id), "session.bin"));
        var accounts = Accounts(); accounts.RemoveAll(a => a.Id == account.Id);
        Json(Path.Combine(Data, "accounts.json"), accounts);
    }
    public static SqliteConnection Open(string path, bool write = false)
    {
        var db = new SqliteConnection(new SqliteConnectionStringBuilder { DataSource = path, Mode = write ? SqliteOpenMode.ReadWriteCreate : SqliteOpenMode.ReadOnly, Pooling = false, DefaultTimeout = 3 }.ToString());
        db.Open(); return db;
    }
    public static string? Meta(SqliteConnection db, string key)
    {
        using var cmd = db.CreateCommand(); cmd.CommandText = "SELECT value FROM meta WHERE key=$key"; cmd.Parameters.AddWithValue("$key", key);
        return cmd.ExecuteScalar() as string;
    }
    public static List<TitleRow> Catalog()
    {
        if (!File.Exists(Manifest)) return [];
        using var json = JsonDocument.Parse(File.ReadAllBytes(Manifest));
        return json.RootElement.GetProperty("titles").EnumerateArray().Select(t => new TitleRow(t.GetProperty("id").GetString()!, t.GetProperty("name").GetString()!, "Pending")).ToList();
    }
    public static bool ImportPrevious(string id)
    {
        if (File.Exists(State(id))) return false;
        foreach (var path in new[] { Path.Combine(Root, "outputs/automation/verified-account/state.sqlite"), Path.Combine(Root, "outputs/automation/state.sqlite") })
        {
            if (!File.Exists(path)) continue;
            using var source = Open(path);
            if (Meta(source, "xuid") != id) continue;
            Directory.CreateDirectory(Folder(id));
            using var target = Open(State(id), true); source.BackupDatabase(target);
            return true;
        }
        return false;
    }
    public static string? Previous(string id)
    {
        foreach (var path in new[] { Path.Combine(Root, "outputs/automation/verified-account/state.sqlite"), Path.Combine(Root, "outputs/automation/state.sqlite") })
        {
            if (!File.Exists(path)) continue;
            using var db = Open(path);
            if (Meta(db, "xuid") == id) return path;
        }
        return null;
    }
    public static List<TitleRow> Rows(string? file)
    {
        if (file == null || !File.Exists(file)) return Catalog();
        using var db = Open(file); using var cmd = db.CreateCommand();
        cmd.CommandText = "SELECT id,name,status,attempts,reason,total FROM jobs ORDER BY rowid";
        using var reader = cmd.ExecuteReader(); var result = new List<TitleRow>();
        while (reader.Read()) result.Add(new(reader.GetString(0), reader.GetString(1), reader.GetString(2), reader.GetInt32(3), reader.GetString(4), reader.GetInt32(5)));
        return result;
    }
    public static void Export(string file, IEnumerable<TitleRow> rows)
    {
        static string Q(string s) => "\"" + (s.Length > 0 && "=+-@\t\r".Contains(s[0]) ? "'" : "") + s.Replace("\"", "\"\"") + "\"";
        var lines = new[] { "ID,Game Title,Status,Attempts,Reason,Achievements" }.Concat(rows.Select(r => string.Join(",", Q(r.Id), Q(r.Name), Q(r.Label), r.Attempts, Q(r.Reason), r.Total)));
        File.WriteAllLines(file, lines, new UTF8Encoding(true));
    }
}
