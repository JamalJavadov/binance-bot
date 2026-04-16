import type { PropsWithChildren } from "react";

type Props = PropsWithChildren<{
  className?: string;
}>;

export function TableContainer({ children, className = "" }: Props) {
  const classes = ["table-scroll", className].filter(Boolean).join(" ");

  return <div className={classes}>{children}</div>;
}
