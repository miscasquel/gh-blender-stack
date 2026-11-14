// Persisted plugin endpoint, in Grasshopper's global settings store.
//
// GH_SettingsServer writes a per-user XML database under the Grasshopper
// app-data folder (typically %APPDATA%\Grasshopper\Stageloom.xml). That is the
// same place Grasshopper keeps its own settings: global to the installation,
// independent of any .gh document, and written before Rhino exits. The typed
// API (GetValue/SetValue/WritePersistentSettings) is small and available in
// Rhino 8, so no separate file format is needed.

using System;
using Grasshopper.Kernel;
using Stageloom.Core;

namespace Stageloom
{
    internal static class EndpointSettings
    {
        internal const string DefaultDatabaseName = "Stageloom";
        internal const string PortKey = "endpoint.port";

        // Tests point this at a throwaway database so a suite run does not
        // overwrite the user's saved endpoint. Production never changes it.
        internal static string DatabaseName = DefaultDatabaseName;

        /// <summary>The saved port, or 0 when none is saved. A malformed or
        /// out-of-range value is reported and treated as unset; the caller
        /// then applies the environment/default precedence.</summary>
        internal static bool TryLoadPort(out int port, out string error)
        {
            port = 0;
            error = null;
            try
            {
                var settings = new GH_SettingsServer(DatabaseName, true);
                int stored = settings.GetValue(PortKey, 0);
                if (stored == 0) return true;
                if (!Endpoint.IsValidPort(stored))
                {
                    error = "saved endpoint port " + stored + " is invalid; ignoring it.";
                    return false;
                }
                port = stored;
                return true;
            }
            catch (Exception ex)
            {
                error = "cannot read the Stageloom endpoint setting: " + FirstLine(ex);
                return false;
            }
        }

        /// <summary>Saves the port for every Grasshopper document and every
        /// later Rhino session. Returns false (with a readable reason) when the
        /// settings database cannot be written; the caller decides whether the
        /// running endpoint still changes.</summary>
        internal static bool SavePort(int port, out string error)
        {
            try
            {
                if (!Endpoint.TryValidatePort(port, out error)) return false;
                var settings = new GH_SettingsServer(DatabaseName, true);
                settings.SetValue(PortKey, port);
                settings.WritePersistentSettings();
                return true;
            }
            catch (Exception ex)
            {
                error = "cannot save the Stageloom endpoint setting: " + FirstLine(ex);
                return false;
            }
        }

        /// <summary>Test/repair helper: forget the saved endpoint. The next
        /// start then uses STAGELOOM_PORT or the default.</summary>
        internal static bool ClearPort(out string error)
        {
            error = null;
            try
            {
                var settings = new GH_SettingsServer(DatabaseName, true);
                settings.DeleteValue(PortKey);
                settings.WritePersistentSettings();
                return true;
            }
            catch (Exception ex)
            {
                error = "cannot clear the Stageloom endpoint setting: " + FirstLine(ex);
                return false;
            }
        }

        private static string FirstLine(Exception ex)
        {
            string message = ex.Message;
            if (string.IsNullOrEmpty(message)) return ex.GetType().Name;
            int newline = message.IndexOf('\n');
            return newline > 0 ? message.Substring(0, newline) : message;
        }
    }
}
