import { motion } from "framer-motion";

interface GlassCardProps {
    children: React.ReactNode;
    className?: string;
    onClick?: () => void;
    hoverable?: boolean;
}

export function GlassCard({ children, className = "", onClick, hoverable = false }: GlassCardProps) {
    return (
        <motion.div
            onClick={onClick}
            whileHover={hoverable && onClick ? { scale: 1.02, y: -2 } : {}}
            whileTap={hoverable && onClick ? { scale: 0.98 } : {}}
            className={`glass rounded-xl p-6 transition-colors duration-200 ${hoverable && onClick ? "cursor-pointer hover:bg-surface-hover/50 hover:border-primary/50" : ""
                } ${className}`}
        >
            {children}
        </motion.div>
    );
}
