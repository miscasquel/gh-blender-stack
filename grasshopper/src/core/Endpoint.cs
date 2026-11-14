// The plugin endpoint: one loopback WebSocket, validated in one place.
//
// The wire protocol is unchanged. This helper only decides which local port the
// Grasshopper client dials and refuses anything that is not loopback, so a
// typo (or a copied URL) can never turn the link into remote networking.
//
// Precedence for the port is: persisted Grasshopper setting > STAGELOOM_PORT
// development override > the default. The pure resolution lives here; the
// persisted store is EndpointSettings in the Grasshopper layer.

using System;
using System.Globalization;

namespace Stageloom.Core
{
    /// <summary>Where the resolved port came from. Kept for diagnostics and
    /// tests; the user sees the canonical endpoint either way.</summary>
    public enum EndpointSource
    {
        Persisted,
        Environment,
        Default,
    }

    public static class Endpoint
    {
        public const int DefaultPort = 59130;
        // Ports below 1024 need elevation on most systems and collide with
        // well-known services; above 65535 there is no socket.
        public const int MinPort = 1024;
        public const int MaxPort = 65535;
        public const string LoopbackHost = "127.0.0.1";
        public const string EnvironmentVariable = "STAGELOOM_PORT";

        public static bool IsValidPort(int port)
        {
            return port >= MinPort && port <= MaxPort;
        }

        public static bool TryValidatePort(int port, out string error)
        {
            if (IsValidPort(port))
            {
                error = null;
                return true;
            }
            error = "Port " + port + " is outside the allowed range "
                    + MinPort + ".." + MaxPort + ".";
            return false;
        }

        /// <summary>Parses a web-form/env value. Only decimal digits are
        /// accepted after trimming, so '+', '-' and grouping separators cannot
        /// smuggle a different number past the range check.</summary>
        public static bool TryParsePort(string text, out int port, out string error)
        {
            port = 0;
            string value = text == null ? "" : text.Trim();
            if (value.Length == 0)
            {
                error = "Port is required.";
                return false;
            }
            for (int i = 0; i < value.Length; i++)
            {
                if (value[i] < '0' || value[i] > '9')
                {
                    error = "Port must be a whole number between "
                            + MinPort + " and " + MaxPort + ".";
                    return false;
                }
            }
            if (!int.TryParse(value, NumberStyles.None, CultureInfo.InvariantCulture, out port))
            {
                error = "Port must be a whole number between "
                        + MinPort + " and " + MaxPort + ".";
                return false;
            }
            return TryValidatePort(port, out error);
        }

        /// <summary>Accepts the loopback address and the name that resolves to
        /// it, and normalizes the name. Everything else — including IPv6 ::1
        /// and 0.0.0.0 — is refused: the product is loopback-only.</summary>
        public static bool TryNormalizeHost(string host, out string normalized, out string error)
        {
            normalized = null;
            string value = host == null ? "" : host.Trim();
            if (value.Length == 0)
            {
                error = "Host is required; use " + LoopbackHost + " or localhost.";
                return false;
            }
            if (value == LoopbackHost
                || string.Equals(value, "localhost", StringComparison.OrdinalIgnoreCase))
            {
                normalized = LoopbackHost;
                error = null;
                return true;
            }
            error = "Host must be " + LoopbackHost + " or localhost; "
                    + "the Stageloom link is loopback-only.";
            return false;
        }

        /// <summary>Validates a host/port pair and returns the canonical
        /// endpoint the socket client uses.</summary>
        public static bool TryCreate(string host, int port,
                                     out string endpoint, out string error)
        {
            endpoint = null;
            string normalized;
            if (!TryNormalizeHost(host, out normalized, out error)) return false;
            if (!TryValidatePort(port, out error)) return false;
            endpoint = "ws://" + normalized + ":" + port.ToString(CultureInfo.InvariantCulture) + "/";
            return true;
        }

        /// <summary>The canonical endpoint for a valid port. Throws for an
        /// invalid port: callers that accept user input use TryCreate.</summary>
        public static string Canonical(int port)
        {
            if (!IsValidPort(port))
                throw new ArgumentOutOfRangeException(
                    "port", port, "Port must be between " + MinPort + " and " + MaxPort + ".");
            return "ws://" + LoopbackHost + ":" + port.ToString(CultureInfo.InvariantCulture) + "/";
        }

        /// <summary>Applies the product precedence. A missing persisted value
        /// is 0; an invalid one is ignored with a warning. An invalid
        /// environment value falls through to the default rather than stopping
        /// the plugin from loading.</summary>
        public static int ResolvePort(int persistedPort, string environmentValue,
                                      out EndpointSource source, out string warning)
        {
            warning = null;
            if (IsValidPort(persistedPort))
            {
                source = EndpointSource.Persisted;
                return persistedPort;
            }
            if (persistedPort != 0)
                warning = "saved endpoint port " + persistedPort + " is invalid; ignoring it.";

            string environment = environmentValue == null ? "" : environmentValue.Trim();
            if (environment.Length > 0)
            {
                int environmentPort;
                string error;
                if (TryParsePort(environment, out environmentPort, out error))
                {
                    source = EndpointSource.Environment;
                    return environmentPort;
                }
                warning = Join(warning,
                    EnvironmentVariable + "='" + environment + "' ignored: " + error);
            }

            source = EndpointSource.Default;
            if (warning != null)
                warning = warning + " Using the default port " + DefaultPort + ".";
            return DefaultPort;
        }

        private static string Join(string first, string second)
        {
            return string.IsNullOrEmpty(first) ? second : first + " " + second;
        }
    }
}
