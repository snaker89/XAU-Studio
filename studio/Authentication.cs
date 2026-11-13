using System.IO;
using System.Net;
using System.Net.Http;
using System.Security.Cryptography;
using System.Text;
using System.Web;
using System.Windows;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.Wpf;
using XboxAuthNet.OAuth;
using XboxAuthNet.OAuth.CodeFlow;
using XboxAuthNet.OAuth.CodeFlow.Parameters;
using XboxAuthNet.XboxLive;
using XboxAuthNet.XboxLive.Requests;
namespace XauStudio;

public interface IXboxSession
{
    Account? Current { get; }
    Task<string> Authorization(CancellationToken ct);
    void Invalidate();
}
public sealed class Authentication : IDisposable, IXboxSession
{
    readonly HttpClient http = new(new HttpClientHandler { AutomaticDecompression = DecompressionMethods.All }) { Timeout = TimeSpan.FromSeconds(45) };
    readonly SemaphoreSlim gate = new(1);
    readonly CodeFlowLiveApiClient oauth;
    string authorization = "";
    DateTimeOffset expires;
    DateTimeOffset retryAfter;
    public Account? Current { get; private set; }
    public string State { get; private set; } = "Nicht angemeldet";
    public Authentication() => oauth = new(XboxGameTitles.XboxAppPC, XboxAuthConstants.XboxScope, http);

    public async Task<Account> Login(Window owner)
    {
        var state = Convert.ToHexString(RandomNumberGenerator.GetBytes(32));
        var verifier = Convert.ToHexString(RandomNumberGenerator.GetBytes(32));
        var challenge = Convert.ToBase64String(SHA256.HashData(Encoding.ASCII.GetBytes(verifier))).TrimEnd('=').Replace('+', '-').Replace('/', '_');
        var uri = oauth.CreateAuthorizeCodeUrl(new CodeFlowAuthorizationParameter { State = state, Prompt = "select_account", CodeChallenge = challenge, CodeChallengeMethod = "S256" });
        var code = await LoginWindow.GetCode(owner, uri, state);
        var response = await oauth.GetAccessToken(new CodeFlowAccessTokenParameter { Code = code, CodeVerifier = verifier }, default);
        return await Establish(response, null);
    }
    public async Task<Account> Restore(Account account)
    {
        State = "Sitzung wird erneuert";
        var response = await oauth.RefreshToken(new CodeFlowRefreshTokenParameter { RefreshToken = Storage.Token(account.Id) }, default);
        return await Establish(response, account.Id);
    }
    async Task<Account> Establish(MicrosoftOAuthResponse response, string? expected)
    {
        var signed = new XboxSignedClient(http);
        var device = await signed.RequestDeviceToken(XboxDeviceTypes.Win32, "0.0.0");
        var result = await signed.SisuAuth(new XboxSisuAuthRequest { AccessToken = response.AccessToken, ClientId = XboxGameTitles.XboxAppPC, DeviceToken = device.Token, RelyingParty = XboxAuthConstants.XboxLiveRelyingParty });
        var token = result.AuthorizationToken;
        var id = token?.XuiClaims?.XboxUserId;
        if (id == null || !id.All(char.IsAsciiDigit) || !token!.Validate() || string.IsNullOrEmpty(token.XuiClaims?.UserHash))
            throw new InvalidDataException("Xbox hat keine gültige Kontositzung geliefert.");
        if (expected != null && id != expected) throw new InvalidDataException("Kontowechsel erkannt. Sitzung wurde verworfen.");
        var account = new Account(id, token.XuiClaims!.UniqueModernGamertag ?? token.XuiClaims.Gamertag ?? "Xbox-Konto …" + id[^4..]);
        var refresh = response.RefreshToken;
        if (string.IsNullOrEmpty(refresh)) throw new InvalidDataException("Microsoft hat keine erneuerbare Sitzung geliefert.");
        Storage.SaveAccount(account, refresh);
        authorization = $"XBL3.0 x={token.XuiClaims.UserHash};{token.Token}";
        expires = DateTimeOffset.Parse(token.ExpireOn!);
        retryAfter = default; Current = account; State = "Angemeldet · direkte Verbindung";
        return account;
    }
    public async Task<string> Authorization(CancellationToken ct)
    {
        await gate.WaitAsync(ct);
        try
        {
            if (Current == null) throw new SessionException();
            if (expires > DateTimeOffset.UtcNow.AddMinutes(5)) return authorization;
            if (retryAfter > DateTimeOffset.UtcNow) throw new SessionException();
            try { await Restore(Current); }
            catch { retryAfter = DateTimeOffset.UtcNow.AddMinutes(5); State = "Anmeldung erforderlich · Konto erneut anmelden"; throw new SessionException(); }
            return authorization;
        }
        finally { gate.Release(); }
    }
    public void Invalidate() { expires = default; State = "Sitzung wird erneut geprüft"; }
    public void Dispose() { http.Dispose(); gate.Dispose(); }
}
public sealed class SessionException : Exception { public SessionException() : base("Anmeldung erforderlich") { } }

public sealed class LoginWindow : Window
{
    readonly WebView2 browser = new();
    readonly TaskCompletionSource<string> completion = new(TaskCreationOptions.RunContinuationsAsynchronously);
    readonly string state;
    readonly string url;
    readonly string browserDir = Path.Combine(Storage.Data, "login-webview");
    LoginWindow(Window owner, string url, string state)
    {
        Owner = owner; Title = "Microsoft-Anmeldung · XAU Studio"; Width = 560; Height = 740;
        WindowStartupLocation = WindowStartupLocation.CenterOwner; Content = browser;
        this.state = state; this.url = url;
        Loaded += async (_, _) =>
        {
            try
            {
                var env = await CoreWebView2Environment.CreateAsync(null, browserDir);
                await browser.EnsureCoreWebView2Async(env);
                // The browser is used only for this login. No password manager or persisted cookies.
                browser.CoreWebView2.Settings.IsPasswordAutosaveEnabled = false;
                browser.CoreWebView2.Settings.IsGeneralAutofillEnabled = false;
                browser.CoreWebView2.Settings.AreDevToolsEnabled = false;
                await browser.CoreWebView2.Profile.ClearBrowsingDataAsync();
                browser.CoreWebView2.NewWindowRequested += (_, e) => e.Handled = true;
                browser.CoreWebView2.NavigationStarting += (_, e) =>
                {
                    if (!Uri.TryCreate(e.Uri, UriKind.Absolute, out var next)) { e.Cancel = true; return; }
                    if (next.Scheme != "https") { e.Cancel = true; return; }
                    if (next.Host != "login.live.com" || next.AbsolutePath != "/oauth20_desktop.srf") return;
                    e.Cancel = true;
                    var query = HttpUtility.ParseQueryString(next.Query);
                    if (query["state"] != state) completion.TrySetException(new InvalidDataException("Ungültige Login-Antwort."));
                    else if (string.IsNullOrEmpty(query["code"])) completion.TrySetException(new SessionException());
                    else completion.TrySetResult(query["code"]!);
                    Close();
                };
                browser.Source = new Uri(url);
            }
            catch (Exception error) { completion.TrySetException(error); Close(); }
        };
        Closed += (_, _) => { completion.TrySetCanceled(); browser.Dispose(); };
    }
    public static async Task<string> GetCode(Window owner, string url, string state)
    {
        var dialog = new LoginWindow(owner, url, state); dialog.Show();
        return await dialog.completion.Task;
    }
}
