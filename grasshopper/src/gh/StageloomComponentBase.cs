// Shared lifecycle for components that register with GhLink.
//
// Registration belongs to one owner, not to each component and its solve: a
// component is registered when Grasshopper adds it to a document and
// unregistered when Grasshopper removes it. The last component leaving stops
// the worker; the next one starts it again. Component-specific setup lives in
// the OnAddedToDocument/OnRemovedFromDocument hooks, so a component never
// overrides the document callbacks themselves.
//
// Components that do not register at all (the attribute components) stay on
// GH_Component; Connection keeps its own endpoint-seeding lifecycle.

using Grasshopper.Kernel;

namespace Stageloom
{
    /// <summary>Base for components whose lifetime is a Stageloom registration:
    /// Stage In, Stage Out and Param. The AddedToDocument/RemovedFromDocument
    /// pair is the only place either registry call happens.</summary>
    public abstract class StageloomComponentBase : GH_Component
    {
        protected StageloomComponentBase(string name, string nickname, string description,
                                        string category, string subCategory)
            : base(name, nickname, description, category, subCategory)
        {
        }

        public override void AddedToDocument(GH_Document document)
        {
            base.AddedToDocument(document);
            GhLink.Register(this);
            OnAddedToDocument(document);
        }

        public override void RemovedFromDocument(GH_Document document)
        {
            base.RemovedFromDocument(document);
            GhLink.Unregister(this);
            OnRemovedFromDocument(document);
        }

        /// <summary>Component-specific setup after the registration exists.
        /// Param uses it to collapse its unused inputs on placement.</summary>
        protected virtual void OnAddedToDocument(GH_Document document)
        {
        }

        /// <summary>Component-specific teardown after the registration is gone.
        /// Nothing currently needs it; it exists so the pair stays symmetric.</summary>
        protected virtual void OnRemovedFromDocument(GH_Document document)
        {
        }

        /// <summary>Drain the link's notes into this solve's runtime remarks.
        /// Notes are shared by the whole link; the components that report them
        /// are the ones that also publish a stage or the endpoint.</summary>
        protected void ReportLinkNotes()
        {
            foreach (string note in GhLink.TakeNotes())
                AddRuntimeMessage(GH_RuntimeMessageLevel.Remark, note);
        }
    }
}
