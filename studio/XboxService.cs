using System.Net;
using System.IO;
using System.Net.Http;
using System.Text;
using System.Text.Json.Nodes;
namespace XauStudio;

public sealed class ServiceException(int status, double retry = 0, int? code = null, string? source = null) : Exception($"Xbox service: HTTP {status}")
{
    public int Status { get; } = status;
    public double Retry { get; } = retry;
    public int? Code { get; } = code;
    public string? SourceName { get; } = source;
}
public sealed class XboxService(IXboxSession auth, HttpMessageHandler? handler = null) : IDisposable
{
    readonly HttpClient http = new(handler ?? new HttpClientHandler { AutomaticDecompression = DecompressionMethods.All }) { Timeout = TimeSpan.FromSeconds(90) };
    DateTimeOffset cooldown;
    public DateTimeOffset Cooldown => cooldown;
    public void SetCooldown(DateTimeOffset value) { if (value > cooldown) cooldown = value; }
    async Task<JsonObject> Request(string url, int contract, CancellationToken ct, object? payload = null)
    {
        if (cooldown > DateTimeOffset.UtcNow) throw new ServiceException(429, (cooldown - DateTimeOffset.UtcNow).TotalSeconds);
        using var request = new HttpRequestMessage(payload == null ? HttpMethod.Get : HttpMethod.Post, url);
        request.Headers.TryAddWithoutValidation("Authorization", await auth.Authorization(ct));
        request.Headers.Add("x-xbl-contract-version", contract.ToString());
        request.Headers.Add("Accept-Language", "en-US");
        request.Headers.TryAddWithoutValidation("User-Agent", "XboxServicesAPI/2021.10.20211005.0 c");
        if (payload != null) request.Content = new StringContent(System.Text.Json.JsonSerializer.Serialize(payload), Encoding.UTF8, "application/json");
        using var response = await http.SendAsync(request, ct);
        var raw = await response.Content.ReadAsStringAsync(ct);
        if (response.StatusCode == HttpStatusCode.NotModified && payload != null) return new();
        if (!response.IsSuccessStatusCode)
        {
            var retry = Math.Max(0, response.Headers.RetryAfter?.Delta?.TotalSeconds ?? (response.Headers.RetryAfter?.Date - DateTimeOffset.UtcNow)?.TotalSeconds ?? 0);
            if (response.StatusCode == HttpStatusCode.TooManyRequests) { retry = Math.Max(30, retry); cooldown = DateTimeOffset.UtcNow.AddSeconds(retry); }
            if (response.StatusCode == HttpStatusCode.Unauthorized) auth.Invalidate();
            int? code = null; string? source = null;
            try { var error = JsonNode.Parse(raw); code = error?["code"]?.GetValue<int>(); source = error?["source"]?.GetValue<string>(); } catch { }
            throw new ServiceException((int)response.StatusCode, retry, code, source);
        }
        return string.IsNullOrWhiteSpace(raw) ? new() : JsonNode.Parse(raw) as JsonObject ?? throw new InvalidDataException("Invalid Xbox response.");
    }
    public Task<JsonObject> Profile(CancellationToken ct) => Request($"https://profile.xboxlive.com/users/xuid({auth.Current!.Id})/profile/settings?settings=Gamertag", 2, ct);
    public async Task<JsonObject> Achievements(string title, CancellationToken ct)
    {
        var result = new JsonArray(); var seen = new HashSet<string>(); string? continuation = null;
        do
        {
            var url = $"https://achievements.xboxlive.com/users/xuid({auth.Current!.Id})/achievements?titleId={title}&maxItems=1000";
            if (continuation != null) url += "&continuationToken=" + Uri.EscapeDataString(continuation);
            var page = await Request(url, 4, ct);
            if (page["achievements"] is not JsonArray items) throw new InvalidDataException("Achievements are missing from the response.");
            foreach (var item in items) result.Add(item?.DeepClone());
            continuation = page["pagingInfo"]?["continuationToken"]?.GetValue<string>();
            if (continuation == "") continuation = null;
            if (continuation != null && !seen.Add(continuation)) throw new InvalidDataException("Wiederholte Ergebnisseite.");
        } while (continuation != null);
        if (result.Count == 0) return await Request($"https://achievements.xboxlive.com/users/xuid({auth.Current!.Id})/titleachievements?titleId={title}&maxItems=1000", 3, ct);
        return new JsonObject { ["achievements"] = result, ["allPagesFetched"] = true };
    }
    public async Task<object> Unlock(string title, CancellationToken ct)
    {
        var data = await Achievements(title, ct);
        var items = data["achievements"]!.AsArray();
        if (items.Count == 0) throw new ServiceException(404);
        if (items.Any(a => a?["progressState"] == null)) throw new ServiceException(400);
        var pending = items.Where(a => a?["progressState"]?.GetValue<string>() != "Achieved").ToList();
        var unsupported = new List<string>();
        foreach (var group in pending.GroupBy(a => a?["serviceConfigId"]?.GetValue<string>()))
        {
            if (!Guid.TryParse(group.Key, out var config) || config == Guid.Empty) throw new ServiceException(400);
            foreach (var chunk in group.Chunk(50))
            {
                ct.ThrowIfCancellationRequested();
                var ids = chunk.Select(a => a!["id"]!.GetValue<string>()).ToList();
                try
                {
                    await Request($"https://achievements.xboxlive.com/users/xuid({auth.Current!.Id})/achievements/{config}/update", 2, ct,
                        new { action = "progressUpdate", titleId = title, serviceConfigId = group.Key, userId = auth.Current.Id, achievements = ids.Select(id => new { id, percentComplete = "100" }) });
                }
                catch (ServiceException ex) when (ex.Status == 400 && ex.Code == 49 && ex.SourceName == "Progress") { unsupported.AddRange(ids); }
            }
        }
        return new { unsupportedAchievementIds = unsupported, submittedAchievements = pending.Count - unsupported.Count };
    }
    public void Dispose() => http.Dispose();
}
