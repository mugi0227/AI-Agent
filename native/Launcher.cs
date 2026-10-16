// Fixed-purpose stdio relay, compiled by the approved Windows .NET Framework compiler.
// No shell and no arbitrary browser-provided command. Configuration stays outside the repo.
using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;
using System.Threading.Tasks;

[DataContract] public sealed class LaunchConfig {
    [DataMember] public string python;
    [DataMember] public string home;
}
public static class Launcher {
    static string Quote(string value) {
        if (String.IsNullOrWhiteSpace(value) || value.Contains("\"") || value.Contains("\r") || value.Contains("\n")) throw new Exception();
        return "\"" + value.TrimEnd('\\') + "\"";
    }
    public static int Main(string[] args) {
        try {
            if (args.Length < 1 || !args[0].StartsWith("chrome-extension://", StringComparison.Ordinal)) return 2;
            string directory = AppDomain.CurrentDomain.BaseDirectory;
            LaunchConfig cfg;
            using (var stream = File.OpenRead(Path.Combine(directory, "native-launcher.json"))) {
                cfg = (LaunchConfig)new DataContractJsonSerializer(typeof(LaunchConfig)).ReadObject(stream);
            }
            if (!Path.IsPathRooted(cfg.python) || !File.Exists(cfg.python) || !Path.IsPathRooted(cfg.home)) return 3;
            var start = new ProcessStartInfo {
                FileName = cfg.python,
                Arguments = "-I -m work_assistant.native --home " + Quote(cfg.home) + " " + Quote(args[0]),
                UseShellExecute = false, CreateNoWindow = true,
                RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true
            };
            using (var child = Process.Start(start)) {
                Task input = Task.Run(async () => { try { await Console.OpenStandardInput().CopyToAsync(child.StandardInput.BaseStream); child.StandardInput.Close(); } catch (IOException) {} });
                Task output = child.StandardOutput.BaseStream.CopyToAsync(Console.OpenStandardOutput());
                Task errors = child.StandardError.BaseStream.CopyToAsync(Stream.Null);
                if (!child.WaitForExit(30000)) { child.Kill(); return 4; }
                output.Wait(); errors.Wait();
                return child.ExitCode;
            }
        } catch { return 1; }
    }
}
