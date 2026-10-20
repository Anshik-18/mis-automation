// Sandbox-only stand-ins for @types/react (not installable here). The real project uses @types/react.
declare module "react" {
  export type ReactNode = any;
  export function useState<S>(init: S | (() => S)): [S, (v: S | ((prev: S) => S)) => void];
  export function useEffect(fn: () => void | (() => void), deps?: readonly unknown[]): void;
  export function useMemo<T>(f: () => T, deps: readonly unknown[]): T;
  export function useCallback<T extends (...a: any[]) => any>(f: T, deps: readonly unknown[]): T;
  export function useRef<T>(init: T): { current: T };
  export function useRef<T>(init: T | null): { current: T | null };
  export function useId(): string;
  export const Fragment: any;
}
declare module "react/jsx-runtime" {
  export namespace JSX {
    type Element = any;
    interface IntrinsicElements { [tag: string]: { [k: string]: any; onChange?: (e: any) => void; onSubmit?: (e: any) => void; onClick?: (e: any) => void; onMouseDown?: (e: any) => void; onKeyDown?: (e: any) => void; onFocus?: (e: any) => void; onBlur?: (e: any) => void; onMouseEnter?: (e: any) => void } }
    interface ElementChildrenAttribute { children: {} }
    interface IntrinsicAttributes { key?: string | number }
  }
  export const jsx: any; export const jsxs: any; export const Fragment: any;
}
declare module "*.css";
