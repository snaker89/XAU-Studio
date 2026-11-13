using System.IO;
using System.Net;
using System.Net.Http;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;
namespace XauStudio;

public static class SelfTests
{
    sealed class Session : IXboxSession
    {
        public Account? Current => new("123", "Test");
        public Task<string> Authorization(CancellationToken ct) => Task.FromResult("test-only");
        public void Invalidate() { }
    }
    sealed class FakeHandler(Func<HttpRequestMessage, HttpResponseMessage> send) : HttpMessageHandler
    {
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken token) => Task.FromResult(send(request));
    }
    static HttpResponseMessage Json(string data, int status = 200) => new((HttpStatusCode)status) { Content = new StringContent(data) };
    static void Check(bool condition, string message) { if (!condition) throw new Exception(message); }
    public static void Run()
    {
        var results = new List<string>();
        var temp = Path.Combine(Path.GetTempPath(), "xau-studio-test-" + Guid.NewGuid()); Directory.CreateDirectory(temp);
        try
        {
            var plain = Encoding.UTF8.GetBytes("test-session");
            var encrypted = ProtectedData.Protect(plain, null, DataProtectionScope.CurrentUser);
            Check(!encrypted.SequenceEqual(plain) && ProtectedData.Unprotect(encrypted, null, DataProtectionScope.CurrentUser).SequenceEqual(plain), "DPAPI"); results.Add("PASS: Windows credential protection");
            var csv = Path.Combine(temp, "titles.csv"); File.WriteAllText(csv, "ID,Game Title\n1,\"Game, One\"\n1,Duplicate\n2,Two\n");
            var parsed = CatalogImport.Parse(csv); Check(parsed.Count == 2 && parsed[0].Name == "Game, One", "CSV");
            File.WriteAllText(csv, "ID,Game Title\n0,Invalid\n");
            try { CatalogImport.Parse(csv); throw new Exception("Invalid ID accepted"); } catch (InvalidDataException) { }
            Storage.Export(csv, [new("1", "=1+1", "Pending")]); Check(File.ReadAllText(csv).Contains("'=1+1"), "CSV formula guard"); results.Add("PASS: CSV quoting, duplicate IDs, validation and formula escaping");
            Task.Run(async () =>
            {
                var calls = 0;
                using (var service = new XboxService(new Session(), new FakeHandler(req =>
                {
                    calls++;
                    Check(req.Headers.GetValues("Authorization").Single() == "test-only", "Auth header");
                    return calls == 1 ? Json("{\"achievements\":[{\"id\":\"1\",\"progressState\":\"Achieved\"}],\"pagingInfo\":{\"continuationToken\":\"next\"}}") : Json("{\"achievements\":[{\"id\":\"2\",\"progressState\":\"Achieved\"}]}");
                })))
                {
                    var data = await service.Achievements("7", default); Check(data["achievements"]!.AsArray().Count == 2 && data["allPagesFetched"]!.GetValue<bool>(), "Pagination");
                }
                results.Add("PASS: achievement pagination and auth header");
                calls = 0;
                using (var service = new XboxService(new Session(), new FakeHandler(_ =>
                {
                    calls++; var response = Json("{}", 429); response.Headers.RetryAfter = new(TimeSpan.FromSeconds(90)); return response;
                })))
                {
                    for (var i = 0; i < 2; i++)
                        try { await service.Profile(default); throw new Exception("429 not propagated"); } catch (ServiceException ex) { Check(ex.Status == 429 && ex.Retry > 80, "Cooldown"); }
                    Check(calls == 1, "Cooldown bypass");
                }
                results.Add("PASS: Retry-After blocks subsequent upstream requests");
                var posts = new List<int>();
                using (var service = new XboxService(new Session(), new FakeHandler(req =>
                {
                    if (req.Method == HttpMethod.Get)
                    {
                        var achievements = new JsonArray();
                        for (var i = 0; i < 51; i++) achievements.Add(new JsonObject { ["id"] = i.ToString(), ["progressState"] = "NotStarted", ["serviceConfigId"] = "11111111-1111-1111-1111-111111111111" });
                        return Json(new JsonObject { ["achievements"] = achievements }.ToJsonString());
                    }
                    var body = JsonNode.Parse(req.Content!.ReadAsStringAsync().GetAwaiter().GetResult())!;
                    Check(body["action"]?.GetValue<string>() == "progressUpdate", "Required progressUpdate action missing");
                    Check(body["userId"]!.GetValue<string>() == "123" && body["titleId"]!.GetValue<string>() == "7", "Update identity");
                    Check(body["serviceConfigId"]!.GetValue<string>() == "11111111-1111-1111-1111-111111111111", "Service config identity");
                    Check(body["achievements"]!.AsArray().All(a => a?["percentComplete"]?.GetValue<string>() == "100" && a["id"] != null), "Achievement payload");
                    posts.Add(body["achievements"]!.AsArray().Count);
                    return posts.Count == 1 ? Json("{\"source\":\"Progress\",\"code\":49}", 400) : Json("{}");
                })))
                {
                    var receipt = JsonNode.Parse(System.Text.Json.JsonSerializer.Serialize(await service.Unlock("7", default)))!;
                    Check(posts.SequenceEqual(new[] { 50, 1 }) && receipt["unsupportedAchievementIds"]!.AsArray().Count == 50, "Chunking and unsupported IDs");
                }
                results.Add("PASS: required progressUpdate schema, identity, 50-item batches and Progress-49 exclusions");
                using var dummy = new XboxService(new Session(), new FakeHandler(_ => Json("{\"profileUsers\":[{\"id\":\"123\"}]}")));
                await using var bridge = new LocalBridge(dummy); using var http = new HttpClient(new HttpClientHandler { UseProxy = false });
                Check((int)(await http.GetAsync(bridge.Url + "/api/profile/me")).StatusCode == 403, "Unprotected bridge");
                http.DefaultRequestHeaders.Add("X-Studio-Key", bridge.Key);
                Check((await http.GetAsync(bridge.Url + "/api/profile/me")).IsSuccessStatusCode, "Bridge valid request");
                http.DefaultRequestHeaders.Add("Origin", "https://example.com");
                Check((int)(await http.GetAsync(bridge.Url + "/api/profile/me")).StatusCode == 403, "Browser origin accepted");
                results.Add("PASS: local bridge key and browser-origin isolation");
            }).GetAwaiter().GetResult();
            results.Add("ALL CHECKS PASSED");
        }
        catch (Exception error) { results.Add("FAIL: " + error); Environment.ExitCode = 1; }
        finally { Directory.CreateDirectory(Storage.Data); File.WriteAllLines(Path.Combine(Storage.Data, "self-test.txt"), results); }
    }
}
