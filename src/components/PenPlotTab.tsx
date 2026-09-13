import { useEffect, useRef, useState } from "react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useToast } from "@/hooks/use-toast";
import { getApiBaseUrl } from "@/services/apiBaseUrl";
import { PenLine, ImagePlus } from "lucide-react";

type LineColor = "black" | "white" | "red" | "blue";
type Background = "none" | "dark-texture" | "light-texture";

const LINE_COLORS: { id: LineColor; label: string; swatch: string }[] = [
  { id: "black", label: "Black", swatch: "#000000" },
  { id: "white", label: "White", swatch: "#FFFFFF" },
  { id: "red", label: "Red", swatch: "#FF0000" },
  { id: "blue", label: "Blue", swatch: "#0000FF" },
];

const BACKGROUNDS: { id: Background; label: string; hint: string }[] = [
  { id: "none", label: "None (plain paper)", hint: "No texture" },
  { id: "dark-texture", label: "Dark texture", hint: "Pairs with white pen" },
  { id: "light-texture", label: "Light texture", hint: "Pairs with red / black / blue" },
];

interface ConvertStats {
  strokes: number;
  points: { before: number; after: number };
  segments: { before: number; after: number };
  pen_down_mm: number;
  pen_up_mm: number;
  estimated_time_s: number;
}

export const PenPlotTab = () => {
  const [imageId, setImageId] = useState<string | null>(null);
  const [meta, setMeta] = useState<string>("No image uploaded yet.");
  const [lineColor, setLineColor] = useState<LineColor>("black");
  const [background, setBackground] = useState<Background>("none");
  const [threshold, setThreshold] = useState(128);
  const [hatchPitch, setHatchPitch] = useState(1.2);
  const [svgUrl, setSvgUrl] = useState<string>("");
  const [stats, setStats] = useState<ConvertStats | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);
  const currentFile = useRef<File | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const { toast } = useToast();

  const readParams = () => ({
    methods: ["hatch"],
    threshold,
    hatch_pitch_mm: hatchPitch,
    // Pen + background are independent display params: any combination works.
    line_color: lineColor,
    background,
  });

  const convert = async (id: string | null = imageId, retry = false) => {
    const target = id ?? imageId;
    if (!target) {
      setStatus("Upload an image first.");
      return;
    }
    setBusy(true);
    setStatus("Converting…");
    try {
      const resp = await fetch(getApiBaseUrl("/v1/convert"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ image_id: target, params: readParams() }),
      });
      const body = await resp.json();
      if (resp.ok) {
        setSvgUrl(body.svg_url);
        setStats(body.stats);
        setWarnings(body.warnings ?? []);
        setStatus("Done.");
        return;
      }
      const code = body.error ? body.error.code : "";
      if (resp.status === 404 && code === "image_not_found" && currentFile.current && !retry) {
        setStatus("Cache expired, re-uploading…");
        await upload(currentFile.current, true);
        return;
      }
      setStatus(`Convert failed (${code}): ${body.error ? body.error.message : ""}`);
    } catch (e) {
      setStatus(String(e));
    } finally {
      setBusy(false);
    }
  };

  const upload = async (file: File, skipConvert = false) => {
    setBusy(true);
    setStatus("Uploading…");
    try {
      const form = new FormData();
      form.append("file", file);
      const resp = await fetch(getApiBaseUrl("/v1/images"), { method: "POST", body: form });
      const body = await resp.json();
      if (!resp.ok) throw new Error(body.error ? `${body.error.code}: ${body.error.message}` : "upload failed");
      setImageId(body.image_id);
      currentFile.current = file;
      setMeta(
        `${body.image_id.slice(0, 12)}… ${body.format} ${body.width}×${body.height}` +
          (body.is_vector ? " (vector)" : "") +
          (body.warnings.length ? ` ⚠ ${body.warnings.join(", ")}` : "")
      );
      if (!skipConvert) await convert(body.image_id);
      else await convert(body.image_id, true);
    } catch (e) {
      setStatus(String(e));
      toast({ title: "Upload failed", description: String(e), variant: "destructive" });
    } finally {
      setBusy(false);
    }
  };

  // Debounced re-convert on pen / background / slider change (independent params).
  useEffect(() => {
    if (!imageId) return;
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => convert(), 500);
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lineColor, background, threshold, hatchPitch]);

  return (
    <div className="grid grid-cols-1 lg:grid-cols-[340px_1fr] gap-4">
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2">
            <PenLine className="h-5 w-5" />
            Pen Plot
          </CardTitle>
          <CardDescription>Image → plotter SVG. Pen and background are independent.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div>
            <Label htmlFor="penplot-file">1. Image</Label>
            <Input
              id="penplot-file"
              ref={fileRef}
              type="file"
              accept="image/*,.svg"
              disabled={busy}
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) upload(f);
              }}
            />
            <p className="mt-1 text-xs text-muted-foreground break-all">{meta}</p>
          </div>

          <div className="rounded-md border bg-muted/20 p-3 space-y-3">
            <div className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
              2. Pen (line color)
            </div>
            <div className="flex flex-wrap gap-2">
              {LINE_COLORS.map((c) => (
                <Button
                  key={c.id}
                  variant={lineColor === c.id ? "default" : "outline"}
                  size="sm"
                  disabled={busy}
                  onClick={() => setLineColor(c.id)}
                  className="gap-2"
                >
                  <span
                    className="inline-block h-3 w-3 rounded-full border"
                    style={{ backgroundColor: c.swatch }}
                  />
                  {c.label}
                </Button>
              ))}
            </div>
          </div>

          <div className="rounded-md border bg-muted/20 p-3 space-y-3">
            <div className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
              3. Background (stretched, white shop frame removed)
            </div>
            <div className="flex flex-wrap gap-2">
              {BACKGROUNDS.map((b) => (
                <Button
                  key={b.id}
                  variant={background === b.id ? "default" : "outline"}
                  size="sm"
                  disabled={busy}
                  onClick={() => setBackground(b.id)}
                >
                  {b.label}
                </Button>
              ))}
            </div>
            <p className="text-xs text-muted-foreground">
              {BACKGROUNDS.find((b) => b.id === background)?.hint}. Suggested pairs: white pen +
              dark texture; red / black / blue pen + light texture.
            </p>
          </div>

          <div className="rounded-md border bg-muted/20 p-3 space-y-3">
            <div className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
              4. Quick pairs
            </div>
            <div className="flex flex-wrap gap-2">
              <Button
                variant="outline"
                size="sm"
                disabled={busy}
                onClick={() => {
                  setLineColor("white");
                  setBackground("dark-texture");
                }}
              >
                White + Dark
              </Button>
              <Button
                variant="outline"
                size="sm"
                disabled={busy}
                onClick={() => {
                  setLineColor("red");
                  setBackground("light-texture");
                }}
              >
                Red + Light
              </Button>
              <Button
                variant="outline"
                size="sm"
                disabled={busy}
                onClick={() => {
                  setLineColor("black");
                  setBackground("light-texture");
                }}
              >
                Black + Light
              </Button>
              <Button
                variant="outline"
                size="sm"
                disabled={busy}
                onClick={() => {
                  setLineColor("blue");
                  setBackground("light-texture");
                }}
              >
                Blue + Light
              </Button>
            </div>
          </div>

          <div className="rounded-md border bg-muted/20 p-3 space-y-3">
            <div className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
              5. Shading
            </div>
            <div>
              <Label htmlFor="penplot-threshold">Threshold ({threshold})</Label>
              <Input
                id="penplot-threshold"
                type="range"
                min={0}
                max={255}
                step={1}
                value={threshold}
                disabled={busy}
                onChange={(e) => setThreshold(parseInt(e.target.value, 10))}
              />
            </div>
            <div>
              <Label htmlFor="penplot-pitch">Hatch pitch mm ({hatchPitch.toFixed(1)})</Label>
              <Input
                id="penplot-pitch"
                type="range"
                min={0.2}
                max={5}
                step={0.1}
                value={hatchPitch}
                disabled={busy}
                onChange={(e) => setHatchPitch(parseFloat(e.target.value))}
              />
            </div>
          </div>

          <Button
            variant="outline"
            size="sm"
            disabled={busy || !imageId}
            onClick={() => convert()}
            className="gap-2"
          >
            <ImagePlus className="h-4 w-4" />
            Re-convert
          </Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle>Preview</CardTitle>
          <CardDescription className="break-all">{status}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {svgUrl ? (
            <img
              src={svgUrl}
              alt="Converted plot preview"
              className="w-full rounded-md border"
              style={{ background: "#fff", minHeight: 200 }}
            />
          ) : (
            <div className="flex min-h-[200px] items-center justify-center rounded-md border text-sm text-muted-foreground">
              Upload an image to see the plot preview.
            </div>
          )}
          {stats && (
            <div className="flex flex-wrap gap-2 text-sm">
              <Badge variant="secondary">strokes {stats.strokes}</Badge>
              <Badge variant="secondary">
                points {stats.points.before} → {stats.points.after}
              </Badge>
              <Badge variant="secondary">pen down {stats.pen_down_mm} mm</Badge>
              <Badge variant="secondary">est {stats.estimated_time_s} s</Badge>
            </div>
          )}
          {warnings.length > 0 && (
            <ul className="list-disc pl-5 text-sm text-amber-600">
              {warnings.map((w) => (
                <li key={w}>{w}</li>
              ))}
            </ul>
          )}
          {svgUrl && (
            <a href={svgUrl} download="plot_optimized.svg" className="text-sm underline">
              download SVG
            </a>
          )}
        </CardContent>
      </Card>
    </div>
  );
};
