/**
 * @receptionai/ui — the shared design system.
 * Consumed by apps/marketing and apps/web. See DESIGN.md.
 */

export { cn } from "./lib/cn";
export { focusRing, focusRingRaised } from "./lib/focus";

export { Button, ButtonLink, buttonVariants } from "./components/Button";
export type { ButtonProps, ButtonLinkProps } from "./components/Button";

export {
  Card,
  CardHeader,
  CardTitle,
  CardDescription,
  CardContent,
  CardFooter,
} from "./components/Card";

export { Badge, badgeVariants } from "./components/Badge";
export type { BadgeProps } from "./components/Badge";

export { LiveBadge } from "./components/LiveBadge";

export { StatTile } from "./components/StatTile";
export type { StatTileProps } from "./components/StatTile";

export { Input } from "./components/Input";
export type { InputProps } from "./components/Input";

export { Select } from "./components/Select";
export type { SelectProps } from "./components/Select";

export { Textarea } from "./components/Textarea";
export type { TextareaProps } from "./components/Textarea";

export { Label } from "./components/Label";
export type { LabelProps } from "./components/Label";

export {
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHead,
  TableCell,
} from "./components/Table";
export type { TableProps } from "./components/Table";

export { Tabs, TabsList, TabsTrigger, TabsContent } from "./components/Tabs";
export type { TabsProps } from "./components/Tabs";

export { Dialog } from "./components/Dialog";
export type { DialogProps } from "./components/Dialog";

export { ToastProvider, useToast } from "./components/Toast";
export type { Toast, ToastVariant } from "./components/Toast";

export { Skeleton } from "./components/Skeleton";

export { Avatar } from "./components/Avatar";
export type { AvatarProps } from "./components/Avatar";

export { Waveform } from "./components/Waveform";
export type { WaveformProps } from "./components/Waveform";

export {
  ConversationCard,
  ConversationResult,
} from "./components/ConversationCard";
export type {
  ConversationCardProps,
  ConversationResultProps,
} from "./components/ConversationCard";

export { TranscriptRow } from "./components/TranscriptRow";
export type { TranscriptRowProps } from "./components/TranscriptRow";
