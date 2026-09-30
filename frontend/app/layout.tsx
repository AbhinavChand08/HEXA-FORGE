import type { Metadata } from 'next';
import './globals.css';
export const metadata: Metadata = {title:'Hexa forge | Network compliance workspace',description:'Inspect, normalize and audit multi-vendor network configurations. Hexa force network auditor.',icons:{icon:'/favicon.svg'}};
export default function RootLayout({children}:{children:React.ReactNode}){return <html lang="en"><body>{children}</body></html>}
