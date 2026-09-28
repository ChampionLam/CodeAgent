import { resolveImageSrc, baseName } from '../../state/live';
import { isVideoRef } from '../../lib/imageRefs';

/**
 * One place that turns a media reference into something visible.
 *
 * Images (and vector / animated files, which an <img> renders fine — SVG keeps
 * its SMIL/CSS animation, GIF and animated WebP run on their own) become <img>;
 * video containers become a muted looping player. Anything unresolvable degrades
 * to a small path chip instead of a broken-image icon.
 */
export function MediaRef({
  value,
  fallback,
  className = 'md-image',
}: {
  value: string;
  fallback?: string;
  className?: string;
}) {
  const src = resolveImageSrc(value);
  if (!src) {
    return <span className="md-image__ref">{fallback || baseName(value) || value}</span>;
  }
  if (isVideoRef(value)) {
    return (
      <video
        className="md-video"
        src={src}
        controls
        loop
        muted
        playsInline
        preload="metadata"
      />
    );
  }
  return <img className={className} src={src} alt={fallback || baseName(value)} loading="lazy" />;
}