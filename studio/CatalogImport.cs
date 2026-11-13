using Microsoft.VisualBasic.FileIO;
using System.IO;
namespace XauStudio;
public static class CatalogImport
{
    public static List<TitleRow> Parse(string file)
    {
        using var parser = new TextFieldParser(file, System.Text.Encoding.UTF8) { TextFieldType = FieldType.Delimited, HasFieldsEnclosedInQuotes = true };
        parser.SetDelimiters(",");
        var header = parser.ReadFields() ?? throw new InvalidDataException("Leere CSV.");
        var idColumn = Array.IndexOf(header, "ID"); var nameColumn = Array.IndexOf(header, "Game Title");
        if (idColumn < 0) throw new InvalidDataException("CSV benötigt die Spalte ID.");
        var result = new Dictionary<string, TitleRow>();
        while (!parser.EndOfData)
        {
            var row = parser.ReadFields()!;
            if (row.Length <= idColumn || !uint.TryParse(row[idColumn], out var id) || id == 0) throw new InvalidDataException("Ungültige Title ID.");
            var key = id.ToString(); var name = nameColumn >= 0 && row.Length > nameColumn ? row[nameColumn] : key;
            result.TryAdd(key, new TitleRow(key, name, "Pending"));
        }
        return result.Values.ToList();
    }
    public static int Import(string file, string account)
    {
        var rows = Parse(file);
        var target = Path.Combine(Storage.Folder(account), "additional-titles.json");
        var existing = Storage.Read<List<TitleRow>>(target) ?? [];
        var ids = existing.Select(t => t.Id).ToHashSet();
        var added = rows.Where(r => ids.Add(r.Id)).ToList();
        existing.AddRange(added); Storage.Json(target, existing); return added.Count;
    }
}
