// One Stage In solve builds geometry from a frame. A malformed frame must not
// escape SolveInstance: Stage In has no response channel, so the component
// reports a readable stage state and stays alive for the next frame.
//
// The guard is separate from StageGeometry so it can be tested without a Rhino
// process: creating Rhino geometry in the console harness needs the native
// runtime, while this class only needs System.

using System;

namespace Stageloom
{
    public static class StageBuildGuard
    {
        /// <summary>Run one build. Returns false with the exception text when the
        /// frame cannot be converted; the caller clears its output and reports
        /// the error.</summary>
        public static bool Try(Action build, out string error)
        {
            try
            {
                build();
                error = "";
                return true;
            }
            catch (Exception ex)
            {
                error = ex.Message;
                return false;
            }
        }
    }
}
