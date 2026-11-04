import { StockDetail } from "../../../components/stock-detail";

export default async function StockPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return <StockDetail key={id} id={id} />;
}
