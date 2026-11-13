using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
namespace XauStudio;

// Only the owned worker knows the per-run key. No browser origins, credential API or remote binding.
public sealed class LocalBridge : IAsyncDisposable
{
    readonly HttpListener listener = new();
    readonly CancellationTokenSource shutdown = new();
    readonly XboxService service;
    readonly Task loop;
    public string Key { get; } = Convert.ToHexString(RandomNumberGenerator.GetBytes(32));
    public string Url { get; }
    public LocalBridge(XboxService service)
    {
        this.service = service;
        using var socket = new TcpListener(IPAddress.Loopback, 0); socket.Start();
        var port = ((IPEndPoint)socket.LocalEndpoint).Port; socket.Stop();
        Url = $"http://127.0.0.1:{port}";
        listener.Prefixes.Add(Url + "/"); listener.Start(); loop = Listen();
    }
    async Task Listen()
    {
        try
        {
            while (!shutdown.IsCancellationRequested)
            {
                var context = await listener.GetContextAsync().WaitAsync(shutdown.Token);
                await Handle(context); // Serial requests keep updates and account state consistent.
            }
        }
        catch (Exception) when (shutdown.IsCancellationRequested) { }
    }
    async Task Handle(HttpListenerContext context)
    {
        try
        {
            var request = context.Request;
            if (!request.IsLocal || request.Headers["Origin"] != null || request.Headers["X-Studio-Key"] != Key)
            { await Send(context, 403, new { error = "Forbidden" }); return; }
            var path = request.Url!.AbsolutePath;
            object result;
            if (path == "/api/profile/me" && request.HttpMethod == "GET") result = await service.Profile(shutdown.Token);
            else if (path.StartsWith("/api/achievements/") && request.HttpMethod == "GET" && uint.TryParse(path.Split('/').Last(), out var title) && title > 0)
                result = await service.Achievements(title.ToString(), shutdown.Token);
            else if (path == "/api/achievements/unlockall" && request.HttpMethod == "POST")
            {
                if (request.ContentLength64 is < 0 or > 4096) { await Send(context, 413, new { error = "Request too large" }); return; }
                using var reader = new System.IO.StreamReader(request.InputStream);
                var json = JsonNode.Parse(await reader.ReadToEndAsync(shutdown.Token));
                if (!uint.TryParse(json?["TitleId"]?.GetValue<string>(), out title) || title == 0) { await Send(context, 400, new { error = "Invalid title" }); return; }
                result = await service.Unlock(title.ToString(), shutdown.Token);
            }
            else { await Send(context, 404, new { error = "Not found" }); return; }
            await Send(context, 200, result);
        }
        catch (ServiceException ex) { await TryError(context, ex.Status, new { error = ex.Message, upstreamStatus = ex.Status, retryAfterSeconds = ex.Retry, xboxErrorCode = ex.Code, xboxErrorSource = ex.SourceName }); }
        catch (SessionException) { await TryError(context, 401, new { error = "Anmeldung erforderlich" }); }
        catch (OperationCanceledException) { await TryError(context, 503, new { error = "Request canceled" }); }
        catch (Exception) { await TryError(context, 502, new { error = "Verbindung oder Antwort ungültig" }); }
    }
    static async Task TryError(HttpListenerContext context, int status, object data) { try { await Send(context, status, data); } catch { context.Response.Abort(); } }
    static async Task Send(HttpListenerContext context, int status, object data)
    {
        var bytes = JsonSerializer.SerializeToUtf8Bytes(data);
        context.Response.StatusCode = status; context.Response.ContentType = "application/json";
        context.Response.ContentLength64 = bytes.Length;
        await context.Response.OutputStream.WriteAsync(bytes); context.Response.Close();
    }
    public async ValueTask DisposeAsync()
    {
        shutdown.Cancel(); listener.Stop();
        await loop; listener.Close(); shutdown.Dispose();
    }
}
