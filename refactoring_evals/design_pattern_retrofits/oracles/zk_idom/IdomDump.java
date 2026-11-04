import java.io.File;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

import org.zkoss.idom.Attribute;
import org.zkoss.idom.Document;
import org.zkoss.idom.Element;
import org.zkoss.idom.Group;
import org.zkoss.idom.Item;
import org.zkoss.idom.Namespace;
import org.zkoss.idom.input.SAXBuilder;

/**
 * Differential oracle for zk's iDOM SAX input layer.
 *
 * Parses every file listed in args[0] (one repo-relative path per line, resolved
 * against args[1]) with several SAXBuilder configurations, and prints one line
 * per (file, configuration): the SHA-256 of a full tree dump (item class, name,
 * text, attributes, namespaces, source line/column), or the exception class and
 * message when parsing fails. A histogram of item classes goes to args[2], so a
 * reviewer can see which factory products the corpus actually exercises.
 *
 * Only public iDOM API that exists both with and without the IDOMFactory
 * abstraction is used, so the same source compiles against both states.
 */
public class IdomDump {
    private static final boolean[][] CONFIGS = {
        // nsaware, smartIgnore
        {true, false}, {false, false}, {true, true}, {false, true},
    };

    private static final Map<String, Integer> histogram = new TreeMap<>();

    public static void main(String[] args) throws Exception {
        List<String> paths = Files.readAllLines(Path.of(args[0]));
        File root = new File(args[1]);
        MessageDigest sha = MessageDigest.getInstance("SHA-256");
        StringBuilder out = new StringBuilder();
        for (String rel : paths) {
            if (rel.isBlank()) continue;
            for (boolean[] cfg : CONFIGS) {
                String label = "ns=" + cfg[0] + ",smart=" + cfg[1];
                String result;
                try {
                    SAXBuilder builder = new SAXBuilder(cfg[0], false, cfg[1]);
                    Document doc = builder.build(new File(root, rel));
                    StringBuilder dump = new StringBuilder();
                    dumpItem(doc, 0, dump);
                    byte[] digest = sha.digest(dump.toString().getBytes(StandardCharsets.UTF_8));
                    result = "ok " + HexFormat.of().formatHex(digest);
                } catch (Throwable ex) {
                    // SAX messages embed "Object@identityHash", which varies per JVM run.
                    String message = String.valueOf(ex.getMessage())
                        .replace('\n', ' ')
                        .replaceAll("@[0-9a-f]+\\b", "@");
                    result = "error " + ex.getClass().getName() + ": " + message;
                }
                out.append(rel).append('\t').append(label).append('\t').append(result).append('\n');
            }
        }
        System.out.print(out);
        StringBuilder hist = new StringBuilder();
        histogram.forEach((k, v) -> hist.append(k).append('\t').append(v).append('\n'));
        Files.writeString(Path.of(args[2]), hist);
    }

    private static void dumpItem(Item item, int depth, StringBuilder dump) {
        String kind = item.getClass().getName();
        histogram.merge(kind, 1, Integer::sum);
        dump.append("  ".repeat(depth)).append(kind)
            .append(" name=").append(item.getName())
            .append(" text=").append(item instanceof Group ? "" : item.getText());
        if (item.getLocator() != null) {
            dump.append(" at=").append(item.getLocator().getLineNumber())
                .append(':').append(item.getLocator().getColumnNumber());
        }
        if (item instanceof Element element) {
            dump.append(" ns=").append(ns(element.getNamespace()));
            for (Attribute attr : element.getAttributeItems()) {
                histogram.merge(attr.getClass().getName(), 1, Integer::sum);
                dump.append(" @").append(ns(attr.getNamespace())).append(attr.getName())
                    .append('=').append(attr.getValue());
            }
        }
        dump.append('\n');
        if (item instanceof Group group) {
            for (Item child : group.getChildren()) {
                dumpItem(child, depth + 1, dump);
            }
        }
    }

    private static String ns(Namespace ns) {
        return ns == null ? "-" : ns.getPrefix() + "|" + ns.getURI() + "|";
    }
}
