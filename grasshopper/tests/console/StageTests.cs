// Layer C, Stage In build guard: a malformed frame becomes a reported stage
// error instead of an exception escaping SolveInstance.
//
//     StageloomStageTests.exe
//
// The guard is deliberately free of Rhino types, so this runs under wine
// without a Rhino process. The real conversion is exercised by the Blender/GH
// live path; here the failure is injected into the same guard Stage In uses.

using System;

namespace Stageloom.Tests.Console
{
    public static class StageTests
    {
        private static int _failures;

        public static int Main(string[] args)
        {
            System.Console.WriteLine("stage in build guard");

            string error;
            bool ok = StageBuildGuard.Try(delegate { }, out error);
            Check("a clean build succeeds", ok && error == "",
                "ok=" + ok + " error=" + error);

            ok = StageBuildGuard.Try(delegate
            {
                throw new InvalidOperationException("face with 2 vertices");
            }, out error);
            Check("a malformed frame is reported",
                !ok && error.IndexOf("face with 2", StringComparison.Ordinal) >= 0,
                "ok=" + ok + " error=" + error);

            System.Console.WriteLine();
            if (_failures == 0)
            {
                System.Console.WriteLine("RESULT: stage in build guard PASS");
                return 0;
            }
            System.Console.WriteLine("RESULT: stage in build guard FAIL (" + _failures + ")");
            return 1;
        }

        private static void Check(string what, bool ok, string detail)
        {
            if (ok)
            {
                System.Console.WriteLine("PASS " + what);
                return;
            }
            _failures++;
            System.Console.WriteLine("FAIL " + what + (detail.Length > 0 ? ": " + detail : ""));
        }
    }
}
