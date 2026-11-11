// The Param node draws a small chevron at the bottom of its capsule. The
// component implements IGH_VariableParameterComponent so the optional inputs
// can be removed and restored; GH's own +/- ZUI would add a second, unrelated
// parameter editor, so the capsule is rendered without it here.

using System.Drawing;
using System.Drawing.Drawing2D;
using System.Windows.Forms;
using Grasshopper.GUI;
using Grasshopper.GUI.Canvas;
using Grasshopper.Kernel.Attributes;

namespace Stageloom
{
    internal sealed class ParamAttributes : GH_ComponentAttributes
    {
        private const float ButtonSize = 11.0f;

        private RectangleF _button;

        public ParamAttributes(ParamComponent owner)
            : base(owner)
        {
        }

        private ParamComponent Param
        {
            get { return (ParamComponent)Owner; }
        }

        protected override void Layout()
        {
            base.Layout();
            RectangleF box = ContentBox;
            if (box.Width <= 0.0f || box.Height <= 0.0f)
            {
                _button = RectangleF.Empty;
                return;
            }
            float cx = box.X + box.Width * 0.5f;
            float cy = box.Bottom;
            _button = new RectangleF(
                cx - ButtonSize * 0.5f,
                cy - ButtonSize * 0.5f,
                ButtonSize,
                ButtonSize);
        }

        protected override void Render(GH_Canvas canvas, Graphics graphics, GH_CanvasChannel channel)
        {
            if (channel != GH_CanvasChannel.Objects)
            {
                base.Render(canvas, graphics, channel);
                return;
            }

            // drawZuiElements: false keeps GH's variable-parameter +/-
            // buttons out of the capsule; this component has one explicit
            // expand/collapse control instead.
            RenderComponentCapsule(canvas, graphics, true, true, true, true, true, false);

            if (!Param.ShowToggle || GH_Canvas.ZoomFadeMedium <= 0 || _button.IsEmpty) return;
            RectangleF bounds = Bounds;
            if (!canvas.Viewport.IsVisible(ref bounds, 10.0f)) return;
            RenderButton(graphics, canvas);
        }

        private void RenderButton(Graphics graphics, GH_Canvas canvas)
        {
            GH_Palette palette = GH_CapsuleRenderEngine.GetImpliedPalette(Param);
            GH_PaletteStyle style = GH_CapsuleRenderEngine.GetImpliedStyle(palette, this);

            bool hover = _button.Contains(CursorCanvas(canvas));
            Color fill = hover ? style.Edge : style.Fill;
            Color edge = style.Edge;
            Color glyph = hover ? style.Fill : style.Text;

            using (var brush = new SolidBrush(fill))
            using (var pen = new Pen(edge, 1.0f))
            {
                graphics.FillEllipse(brush, _button);
                graphics.DrawEllipse(pen, _button);
            }

            float cx = _button.X + _button.Width * 0.5f;
            float cy = _button.Y + _button.Height * 0.5f;
            PointF[] chevron = Param.HasHiddenInputs
                ? new[]
                {
                    new PointF(cx - 2.5f, cy - 1.2f),
                    new PointF(cx, cy + 1.5f),
                    new PointF(cx + 2.5f, cy - 1.2f),
                }
                : new[]
                {
                    new PointF(cx - 2.5f, cy + 1.5f),
                    new PointF(cx, cy - 1.2f),
                    new PointF(cx + 2.5f, cy + 1.5f),
                };

            SmoothingMode previous = graphics.SmoothingMode;
            graphics.SmoothingMode = SmoothingMode.AntiAlias;
            try
            {
                using (var pen = new Pen(glyph, 1.5f))
                {
                    pen.StartCap = LineCap.Round;
                    pen.EndCap = LineCap.Round;
                    graphics.DrawLines(pen, chevron);
                }
            }
            finally
            {
                graphics.SmoothingMode = previous;
            }
        }

        private static PointF CursorCanvas(GH_Canvas canvas)
        {
            Point client = canvas.PointToClient(Cursor.Position);
            return canvas.Viewport.UnprojectPoint(new PointF(client.X, client.Y));
        }

        public override bool IsPickRegion(PointF point)
        {
            return (Param.ShowToggle && _button.Contains(point)) || base.IsPickRegion(point);
        }

        public override GH_ObjectResponse RespondToMouseDown(GH_Canvas sender, GH_CanvasMouseEvent e)
        {
            if (Param.ShowToggle
                && e.Button == MouseButtons.Left
                && GH_Canvas.ZoomFadeMedium > 0
                && !_button.IsEmpty
                && _button.Contains(e.CanvasLocation))
            {
                Param.ToggleInputs();
                sender.Invalidate();
                return GH_ObjectResponse.Handled;
            }
            return base.RespondToMouseDown(sender, e);
        }

        public override void SetupTooltip(PointF canvasPoint, GH_TooltipDisplayEventArgs e)
        {
            if (Param.ShowToggle && GH_Canvas.ZoomFadeMedium > 0 && _button.Contains(canvasPoint))
            {
                e.Title = Param.HasHiddenInputs ? "Show all inputs" : "Hide unused inputs";
                e.Text = Param.HasHiddenInputs
                    ? "Reveal the optional fields. Inputs with values or wires stay visible."
                    : "Hide optional inputs that have no value and no wire.";
                return;
            }
            base.SetupTooltip(canvasPoint, e);
        }
    }
}
