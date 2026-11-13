using System.IO;
using System.IO.Compression;
using System.Security.Cryptography;
namespace XauStudio;
public static class PortableAssets
{
    const string Resource = "XauStudio.PortablePayload.zip";
    public static bool Enabled => typeof(PortableAssets).Assembly.GetManifestResourceInfo(Resource) != null;
    public static string Data => Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "XauStudioPortable", "data");
    public static string Extract()
    {
        using var payload = typeof(PortableAssets).Assembly.GetManifestResourceStream(Resource)!;
        var hash = Convert.ToHexString(SHA256.HashData(payload))[..16]; payload.Position = 0;
        var root = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "XauStudioPortable", "runtime-" + hash);
        if (File.Exists(Path.Combine(root, "ready.txt"))) return root;
        var staging = root + "." + Guid.NewGuid().ToString("N");
        Directory.CreateDirectory(staging);
        using (var zip = new ZipArchive(payload, ZipArchiveMode.Read)) zip.ExtractToDirectory(staging);
        File.WriteAllText(Path.Combine(staging, "ready.txt"), hash);
        try { Directory.Move(staging, root); }
        catch (IOException) when (File.Exists(Path.Combine(root, "ready.txt"))) { Directory.Delete(staging, true); }
        return root;
    }
}
