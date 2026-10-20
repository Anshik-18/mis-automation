import { handleMisRequest } from "../../../../src/server/handlers";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET(req: Request): Promise<Response> { return handleMisRequest(req); }
export async function POST(req: Request): Promise<Response> { return handleMisRequest(req); }
export async function PUT(req: Request): Promise<Response> { return handleMisRequest(req); }
export async function DELETE(req: Request): Promise<Response> { return handleMisRequest(req); }
export async function PATCH(req: Request): Promise<Response> { return handleMisRequest(req); }
