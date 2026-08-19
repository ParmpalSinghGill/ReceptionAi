"use client";

import * as React from "react";
import { cn } from "../lib/cn";
import { focusRing } from "../lib/focus";

/**
 * Tabs — the WAI-ARIA Tabs pattern, implemented directly so the package has
 * no Radix dependency.
 *
 * Keyboard contract (automatic activation):
 *   ArrowLeft / ArrowRight  move and select, wrapping at the ends
 *   Home / End              jump to first / last
 * Roving tabindex: only the selected tab is in the tab order, so Tab moves
 * OUT of the tablist to the panel rather than through every tab.
 */

interface TabsContextValue {
  value: string;
  setValue: (v: string) => void;
  baseId: string;
  register: (v: string) => void;
  order: React.MutableRefObject<string[]>;
}

const TabsContext = React.createContext<TabsContextValue | null>(null);

function useTabs(component: string): TabsContextValue {
  const ctx = React.useContext(TabsContext);
  if (!ctx) throw new Error(`<${component}> must be used inside <Tabs>`);
  return ctx;
}

export interface TabsProps extends React.HTMLAttributes<HTMLDivElement> {
  /** Controlled selected value. */
  value?: string;
  /** Uncontrolled initial value. */
  defaultValue: string;
  onValueChange?: (value: string) => void;
}

export function Tabs({
  value: controlled,
  defaultValue,
  onValueChange,
  className,
  children,
  ...props
}: TabsProps) {
  const [uncontrolled, setUncontrolled] = React.useState(defaultValue);
  const value = controlled ?? uncontrolled;
  const baseId = React.useId();
  const order = React.useRef<string[]>([]);

  const setValue = React.useCallback(
    (v: string) => {
      if (controlled === undefined) setUncontrolled(v);
      onValueChange?.(v);
    },
    [controlled, onValueChange],
  );

  // Tabs register in render order so arrow keys follow visual order without
  // the list having to query the DOM.
  const register = React.useCallback((v: string) => {
    if (!order.current.includes(v)) order.current.push(v);
  }, []);

  const ctx = React.useMemo(
    () => ({ value, setValue, baseId, register, order }),
    [value, setValue, baseId, register],
  );

  return (
    <TabsContext.Provider value={ctx}>
      <div className={cn("flex flex-col gap-4", className)} {...props}>
        {children}
      </div>
    </TabsContext.Provider>
  );
}

export function TabsList({
  className,
  children,
  ...props
}: React.HTMLAttributes<HTMLDivElement> & { "aria-label"?: string }) {
  return (
    <div
      role="tablist"
      className={cn(
        "inline-flex items-center gap-1 rounded-pill border border-line bg-bg-inset p-1",
        className,
      )}
      {...props}
    >
      {children}
    </div>
  );
}

export interface TabsTriggerProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  value: string;
}

export const TabsTrigger = React.forwardRef<
  HTMLButtonElement,
  TabsTriggerProps
>(function TabsTrigger({ className, value, children, ...props }, ref) {
  const { value: selected, setValue, baseId, register, order } = useTabs("TabsTrigger");
  register(value);
  const isSelected = selected === value;

  function move(delta: number) {
    const list = order.current;
    const i = list.indexOf(selected);
    // Wrap with a positive modulo so ArrowLeft from index 0 lands on the last.
    const next = list[(i + delta + list.length) % list.length];
    if (next) {
      setValue(next);
      document.getElementById(`${baseId}-tab-${next}`)?.focus();
    }
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLButtonElement>) {
    const list = order.current;
    switch (e.key) {
      case "ArrowRight":
        e.preventDefault();
        move(1);
        break;
      case "ArrowLeft":
        e.preventDefault();
        move(-1);
        break;
      case "Home": {
        e.preventDefault();
        const first = list[0];
        if (first) {
          setValue(first);
          document.getElementById(`${baseId}-tab-${first}`)?.focus();
        }
        break;
      }
      case "End": {
        e.preventDefault();
        const last = list[list.length - 1];
        if (last) {
          setValue(last);
          document.getElementById(`${baseId}-tab-${last}`)?.focus();
        }
        break;
      }
    }
  }

  return (
    <button
      ref={ref}
      type="button"
      role="tab"
      id={`${baseId}-tab-${value}`}
      aria-selected={isSelected}
      aria-controls={`${baseId}-panel-${value}`}
      // Roving tabindex: unselected tabs leave the tab order entirely.
      tabIndex={isSelected ? 0 : -1}
      onClick={() => setValue(value)}
      onKeyDown={onKeyDown}
      className={cn(
        "rounded-pill px-4 py-1.5 font-sans text-body-sm font-medium",
        "transition-colors duration-micro ease-brand",
        isSelected
          ? "bg-accent text-accent-fg"
          : "text-fg-muted hover:bg-bg-raised hover:text-fg",
        focusRing,
        className,
      )}
      {...props}
    >
      {children}
    </button>
  );
});

export interface TabsContentProps
  extends React.HTMLAttributes<HTMLDivElement> {
  value: string;
}

export function TabsContent({
  className,
  value,
  children,
  ...props
}: TabsContentProps) {
  const { value: selected, baseId } = useTabs("TabsContent");
  if (selected !== value) return null;
  return (
    <div
      role="tabpanel"
      id={`${baseId}-panel-${value}`}
      aria-labelledby={`${baseId}-tab-${value}`}
      // The panel is focusable so Tab from the selected tab lands on content.
      tabIndex={0}
      className={cn("outline-none focus-visible:ring-2 focus-visible:ring-accent", className)}
      {...props}
    >
      {children}
    </div>
  );
}
