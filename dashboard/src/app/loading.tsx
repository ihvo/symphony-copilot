export default function Loading() {
  return (
    <div className="animate-pulse space-y-8">
      <div className="h-8 w-48 bg-zinc-100 rounded" />
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
        {Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className="h-24 bg-zinc-100 rounded-xl" />
        ))}
      </div>
      <div className="h-64 bg-zinc-100 rounded-xl" />
    </div>
  );
}
